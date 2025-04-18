import posthogEE from '@posthog/ee/exports'
import { customEvent, EventType, eventWithTime, fullSnapshotEvent, IncrementalSource } from '@posthog/rrweb-types'
import { captureException } from '@sentry/react'
import { gunzipSync, strFromU8, strToU8 } from 'fflate'
import {
    actions,
    afterMount,
    beforeUnmount,
    connect,
    defaults,
    kea,
    key,
    listeners,
    path,
    props,
    reducers,
    selectors,
} from 'kea'
import { loaders } from 'kea-loaders'
import { subscriptions } from 'kea-subscriptions'
import api from 'lib/api'
import { FEATURE_FLAGS } from 'lib/constants'
import { Dayjs, dayjs } from 'lib/dayjs'
import { featureFlagLogic, FeatureFlagsSet } from 'lib/logic/featureFlagLogic'
import { isObject } from 'lib/utils'
import { chainToElements } from 'lib/utils/elements-chain'
import { eventUsageLogic } from 'lib/utils/eventUsageLogic'
import posthog from 'posthog-js'
import { compressedEventWithTime } from 'posthog-js/lib/src/extensions/replay/sessionrecording'
import { RecordingComment } from 'scenes/session-recordings/player/inspector/playerInspectorLogic'
import { teamLogic } from 'scenes/teamLogic'

import { HogQLQuery, NodeKind } from '~/queries/schema/schema-general'
import { hogql } from '~/queries/utils'
import {
    EncodedRecordingSnapshot,
    RecordingEventsFilters,
    RecordingEventType,
    RecordingReportLoadTimes,
    RecordingSegment,
    RecordingSnapshot,
    SessionPlayerData,
    SessionRecordingId,
    SessionRecordingSnapshotParams,
    SessionRecordingSnapshotSource,
    SessionRecordingSnapshotSourceResponse,
    SessionRecordingType,
    SessionRecordingUsageType,
    SnapshotSourceType,
} from '~/types'

import { PostHogEE } from '../../../../@posthog/ee/types'
import { ExportedSessionRecordingFileV2 } from '../file-playback/types'
import type { sessionRecordingDataLogicType } from './sessionRecordingDataLogicType'
import { createSegments, mapSnapshotsToWindowId } from './utils/segmenter'

const IS_TEST_MODE = process.env.NODE_ENV === 'test'
const TWENTY_FOUR_HOURS_IN_MS = 24 * 60 * 60 * 1000 // +- before and after start and end of a recording to query for.
const DEFAULT_REALTIME_POLLING_MILLIS = 3000
const DEFAULT_V2_POLLING_INTERVAL_MS = 10000
export const MUTATION_CHUNK_SIZE = 5000 // Maximum number of mutations per chunk

let postHogEEModule: PostHogEE

export interface ViewportResolution {
    width: string
    height: string
    href: string
}

function isRecordingSnapshot(x: unknown): x is RecordingSnapshot {
    return typeof x === 'object' && x !== null && 'type' in x && 'timestamp' in x
}

// when we capture in a loop, we don't want to capture the same error twice
// since we loop over large datasets we risk capturing the same error multiple times
// so we use a set to throttle the errors
const THROTTLE_CAPTURE_KEY = new Set<string>()
function throttleCapture(key: string, fn: () => void): void {
    if (!THROTTLE_CAPTURE_KEY.has(key)) {
        fn()
        THROTTLE_CAPTURE_KEY.add(key)
    }
}
// only for testing
export function clearThrottle(): void {
    THROTTLE_CAPTURE_KEY.clear()
}

export function patchMetaEventIntoWebData(
    snapshots: RecordingSnapshot[],
    viewportForTimestamp: (timestamp: number) => ViewportResolution | undefined,
    sessionRecordingId: string
): RecordingSnapshot[] {
    // Iterate in reverse order so we can modify the array while iterating
    for (let i = snapshots.length - 1; i >= 0; i--) {
        const snapshot = snapshots[i]
        if (snapshot.type !== EventType.FullSnapshot) {
            continue
        }

        const previousEvent = snapshots[i - 1]
        const previousEventIsMeta = previousEvent?.type === EventType.Meta
        if (previousEventIsMeta) {
            continue
        }

        const viewport = viewportForTimestamp(snapshot.timestamp)
        const thereIsNoViewport = !viewport || !viewport.width || !viewport.height
        if (thereIsNoViewport) {
            throttleCapture(`${sessionRecordingId}-no-viewport-found`, () => {
                posthog.captureException(new Error('No event viewport or meta snapshot found for full snapshot'), {
                    snapshot,
                })
            })
        } else {
            snapshots.splice(i, 0, {
                type: EventType.Meta,
                timestamp: snapshot.timestamp,
                windowId: snapshot.windowId,
                data: {
                    width: parseInt(viewport.width, 10),
                    height: parseInt(viewport.height, 10),
                    href: viewport.href || 'unknown',
                },
            })
        }
    }

    return snapshots
}

/*
 there was a bug in mobile SDK that didn't consistently send a meta event with a full snapshot.
 rrweb player hides itself until it has seen the meta event 🤷
 but we can patch a meta event into the recording data to make it work
*/
function patchMetaEventIntoMobileData(
    parsedLines: RecordingSnapshot[],
    sessionRecordingId: string
): RecordingSnapshot[] {
    let fullSnapshotIndex: number = -1
    let metaIndex: number = -1
    try {
        fullSnapshotIndex = parsedLines.findIndex((l) => l.type === EventType.FullSnapshot)
        metaIndex = parsedLines.findIndex((l) => l.type === EventType.Meta)

        // then we need to patch the meta event into the snapshot data
        if (fullSnapshotIndex > -1 && metaIndex === -1) {
            const fullSnapshot = parsedLines[fullSnapshotIndex] as RecordingSnapshot & fullSnapshotEvent & eventWithTime
            // a full snapshot (particularly from the mobile transformer) has a relatively fixed structure,
            // but the types exposed by rrweb don't quite cover what we need , so...
            const mainNode = fullSnapshot.data.node as any
            const targetNode = mainNode.childNodes[1].childNodes[1].childNodes[0]
            const { width, height } = targetNode.attributes
            const metaEvent: RecordingSnapshot = {
                windowId: fullSnapshot.windowId,
                type: EventType.Meta,
                timestamp: fullSnapshot.timestamp,
                data: {
                    href: getHrefFromSnapshot(fullSnapshot) || '',
                    width,
                    height,
                },
            }
            parsedLines.splice(fullSnapshotIndex, 0, metaEvent)
        }
    } catch (e) {
        throttleCapture(`${sessionRecordingId}-missing-mobile-meta-patching`, () => {
            posthog.captureException(e, {
                tags: { feature: 'session-recording-missing-mobile-meta-patching' },
                extra: { fullSnapshotIndex, metaIndex },
            })
        })
    }

    return parsedLines
}

function hasAnyWireframes(snapshotData: Record<string, any>[]): boolean {
    return snapshotData.some((d) => {
        return isObject(d.data) && 'wireframes' in d.data
    })
}

function isCompressedEvent(ev: unknown): ev is compressedEventWithTime {
    return typeof ev === 'object' && ev !== null && 'cv' in ev
}

function unzip(compressedStr: string | undefined): any {
    if (!compressedStr) {
        return undefined
    }
    return JSON.parse(strFromU8(gunzipSync(strToU8(compressedStr, true))))
}

/**
 *
 * takes an event that might be from web, might be from mobile,
 * and might be partially compressed,
 * and decompresses it when possible
 *
 * you can't return a union of `KnownType | unknown`
 * so even though this returns `eventWithTime | unknown`
 * it has to be typed as only unknown
 *
 * KLUDGE: we shouldn't need so many type assertions on ev.data but TS is not smart enough to figure it out
 */
function decompressEvent(ev: unknown, sessionRecordingId: string): unknown {
    try {
        if (isCompressedEvent(ev)) {
            if (ev.cv === '2024-10') {
                if (ev.type === EventType.FullSnapshot && typeof ev.data === 'string') {
                    return {
                        ...ev,
                        data: unzip(ev.data),
                    }
                } else if (
                    ev.type === EventType.IncrementalSnapshot &&
                    typeof ev.data === 'object' &&
                    'source' in ev.data
                ) {
                    if (ev.data.source === IncrementalSource.StyleSheetRule) {
                        return {
                            ...ev,
                            data: {
                                ...ev.data,
                                source: IncrementalSource.StyleSheetRule,
                                adds: unzip(ev.data.adds),
                                removes: unzip(ev.data.removes),
                            },
                        }
                    } else if (ev.data.source === IncrementalSource.Mutation && 'texts' in ev.data) {
                        return {
                            ...ev,
                            data: {
                                ...ev.data,
                                source: IncrementalSource.Mutation,
                                adds: unzip(ev.data.adds),
                                removes: unzip(ev.data.removes),
                                texts: unzip(ev.data.texts),
                                attributes: unzip(ev.data.attributes),
                            },
                        }
                    }
                }
            } else {
                throttleCapture(`${sessionRecordingId}-unknown-compressed-event-version`, () => {
                    posthog.captureException(new Error('Unknown compressed event version'), {
                        feature: 'session-recording-compressed-event-decompression',
                        compressedEvent: ev,
                        compressionVersion: ev.cv,
                    })
                })
                // probably unplayable but we don't know how to decompress it
                return ev
            }
        }
        return ev
    } catch (e) {
        throttleCapture(`${sessionRecordingId}-unknown-compressed-event-version`, () => {
            posthog.captureException((e as Error) || new Error('Could not decompress event'), {
                feature: 'session-recording-compressed-event-decompression',
                compressedEvent: ev,
            })
        })
        return ev
    }
}

/**
 * We can receive data in one of multiple formats, so we treat it as unknown
 * And if we can't process it force it into eventWithTime
 *
 * If it can't be case as eventWithTime by this point then it's probably not a valid event anyway
 */
function coerceToEventWithTime(d: unknown, withMobileTransformer: boolean, sessionRecordingId: string): eventWithTime {
    // we decompress first so that we could support partial compression on mobile in future
    const currentEvent = decompressEvent(d, sessionRecordingId)
    return withMobileTransformer
        ? postHogEEModule?.mobileReplay?.transformEventToWeb(currentEvent) || (currentEvent as eventWithTime)
        : (currentEvent as eventWithTime)
}

export function chunkMutationSnapshot(snapshot: RecordingSnapshot): RecordingSnapshot[] {
    if (
        snapshot.type !== EventType.IncrementalSnapshot ||
        !('data' in snapshot) ||
        !snapshot.data ||
        typeof snapshot.data !== 'object' ||
        !('source' in snapshot.data) ||
        snapshot.data.source !== IncrementalSource.Mutation ||
        !('adds' in snapshot.data) ||
        !Array.isArray(snapshot.data.adds) ||
        snapshot.data.adds.length <= MUTATION_CHUNK_SIZE
    ) {
        return [snapshot]
    }

    const chunks: RecordingSnapshot[] = []
    const { adds, removes, texts, attributes } = snapshot.data
    const totalAdds = adds.length
    const chunksCount = Math.ceil(totalAdds / MUTATION_CHUNK_SIZE)

    for (let i = 0; i < chunksCount; i++) {
        const startIdx = i * MUTATION_CHUNK_SIZE
        const endIdx = Math.min((i + 1) * MUTATION_CHUNK_SIZE, totalAdds)
        const isFirstChunk = i === 0
        const isLastChunk = i === chunksCount - 1

        const chunkSnapshot: RecordingSnapshot = {
            ...snapshot,
            timestamp: snapshot.timestamp,
            data: {
                ...snapshot.data,
                adds: adds.slice(startIdx, endIdx),
                // Keep removes in the first chunk only
                removes: isFirstChunk ? removes : [],
                // Keep texts and attributes in the last chunk only
                texts: isLastChunk ? texts : [],
                attributes: isLastChunk ? attributes : [],
            },
        }

        // If delay was present in the original snapshot, increment it by 1 for each chunk
        if ('delay' in snapshot) {
            chunkSnapshot.delay = snapshot.delay || 0
        }

        chunks.push(chunkSnapshot)
    }

    return chunks
}

export const parseEncodedSnapshots = async (
    items: (RecordingSnapshot | EncodedRecordingSnapshot | string)[],
    sessionId: string,
    withMobileTransformer: boolean = true
): Promise<RecordingSnapshot[]> => {
    if (!postHogEEModule) {
        postHogEEModule = await posthogEE()
    }

    const lineCount = items.length
    const unparseableLines: string[] = []
    let isMobileSnapshots = false

    const parsedLines: RecordingSnapshot[] = items.flatMap((l) => {
        if (!l) {
            // blob files have an empty line at the end
            return []
        }
        try {
            let snapshotLine: { windowId: string } | EncodedRecordingSnapshot
            if (typeof l === 'string') {
                // is loaded from blob or realtime storage
                snapshotLine = JSON.parse(l) as EncodedRecordingSnapshot
                if (Array.isArray(snapshotLine)) {
                    snapshotLine = {
                        windowId: snapshotLine[0],
                        data: [snapshotLine[1]],
                    }
                }
            } else {
                // is loaded from file export
                snapshotLine = l
            }
            let snapshotData: ({ windowId: string } | EncodedRecordingSnapshot)[]
            if (isRecordingSnapshot(snapshotLine)) {
                // is loaded from file export
                snapshotData = [snapshotLine]
            } else {
                // is loaded from blob or realtime storage
                snapshotData = snapshotLine['data']
            }

            if (!isMobileSnapshots) {
                isMobileSnapshots = hasAnyWireframes(snapshotData)
            }

            return snapshotData.flatMap((d: unknown) => {
                const snap = coerceToEventWithTime(d, withMobileTransformer, sessionId)

                const baseSnapshot: RecordingSnapshot = {
                    windowId: snapshotLine['window_id'] || snapshotLine['windowId'],
                    ...snap,
                }

                // Apply chunking to the snapshot if needed
                return chunkMutationSnapshot(baseSnapshot)
            })
        } catch (e) {
            if (typeof l === 'string') {
                unparseableLines.push(l)
            }
            return []
        }
    })

    if (unparseableLines.length) {
        const extra = {
            playbackSessionId: sessionId,
            totalLineCount: lineCount,
            unparseableLinesCount: unparseableLines.length,
            exampleLines: unparseableLines.slice(0, 3),
        }
        throttleCapture(`${sessionId}-unparseable-lines`, () => {
            posthog.capture('session recording had unparseable lines', {
                ...extra,
                feature: 'session-recording-snapshot-processing',
            })
        })
    }

    return isMobileSnapshots ? patchMetaEventIntoMobileData(parsedLines, sessionId) : parsedLines
}

const getHrefFromSnapshot = (snapshot: unknown): string | undefined => {
    return isObject(snapshot) && 'data' in snapshot
        ? (snapshot.data as any)?.href || (snapshot.data as any)?.payload?.href
        : undefined
}

/*
    cyrb53 (c) 2018 bryc (github.com/bryc)
    License: Public domain. Attribution appreciated.
    A fast and simple 53-bit string hash function with decent collision resistance.
    Largely inspired by MurmurHash2/3, but with a focus on speed/simplicity.
*/
const cyrb53 = function (str: string, seed = 0): number {
    let h1 = 0xdeadbeef ^ seed,
        h2 = 0x41c6ce57 ^ seed
    for (let i = 0, ch; i < str.length; i++) {
        ch = str.charCodeAt(i)
        h1 = Math.imul(h1 ^ ch, 2654435761)
        h2 = Math.imul(h2 ^ ch, 1597334677)
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507)
    h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909)
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507)
    h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909)
    return 4294967296 * (2097151 & h2) + (h1 >>> 0)
}

export const deduplicateSnapshots = (snapshots: RecordingSnapshot[] | null): RecordingSnapshot[] => {
    const seenHashes: Set<string> = new Set()

    return (snapshots ?? [])
        .filter((snapshot) => {
            // For a multitude of reasons, there can be duplicate snapshots in the same recording.
            // we have to stringify the snapshot to compare it to other snapshots.
            // so we can filter by storing them all in a set

            // we can see duplicates that only differ by delay - these still count as duplicates
            // even though the delay would hide that
            const { delay: _delay, ...delayFreeSnapshot } = snapshot
            // we check each item multiple times as new snapshots come in
            // so store the computer value on the object to save recalculating it so much
            const key = (snapshot as any).seen || cyrb53(JSON.stringify(delayFreeSnapshot))
            ;(snapshot as any).seen = key

            if (seenHashes.has(key)) {
                return false
            }
            seenHashes.add(key)
            return true
        })
        .sort((a, b) => a.timestamp - b.timestamp)
}

const generateRecordingReportDurations = (cache: Record<string, any>): RecordingReportLoadTimes => {
    return {
        metadata: cache.metadataLoadDuration || Math.round(performance.now() - cache.metaStartTime),
        snapshots: cache.snapshotsLoadDuration || Math.round(performance.now() - cache.snapshotsStartTime),
        events: cache.eventsLoadDuration || Math.round(performance.now() - cache.eventsStartTime),
        firstPaint: cache.firstPaintDuration,
    }
}

const resetTimingsCache = (cache: Record<string, any>): void => {
    cache.metaStartTime = null
    cache.metadataLoadDuration = null
    cache.snapshotsStartTime = null
    cache.snapshotsLoadDuration = null
    cache.eventsStartTime = null
    cache.eventsLoadDuration = null
    cache.firstPaintDuration = null
}

export interface SessionRecordingDataLogicProps {
    sessionRecordingId: SessionRecordingId
    realTimePollingIntervalMilliseconds?: number
}

async function processEncodedResponse(
    encodedResponse: (EncodedRecordingSnapshot | string)[],
    props: SessionRecordingDataLogicProps,
    featureFlags: FeatureFlagsSet
): Promise<{ transformed: RecordingSnapshot[]; untransformed: RecordingSnapshot[] | null }> {
    let untransformed: RecordingSnapshot[] | null = null

    const transformed = await parseEncodedSnapshots(encodedResponse, props.sessionRecordingId)

    if (featureFlags[FEATURE_FLAGS.SESSION_REPLAY_EXPORT_MOBILE_DATA]) {
        untransformed = await parseEncodedSnapshots(
            encodedResponse,
            props.sessionRecordingId,
            false // don't transform mobile data
        )
    }

    return { transformed, untransformed }
}

const getSourceKey = (source: SessionRecordingSnapshotSource): string => {
    // realtime sources vary so blob_key is not always present and is either null or undefined...
    // we only care about key when not realtime
    // and we'll always have a key when not realtime
    return `${source.source}-${source.blob_key || source.source}`
}

export const sessionRecordingDataLogic = kea<sessionRecordingDataLogicType>([
    path((key) => ['scenes', 'session-recordings', 'sessionRecordingDataLogic', key]),
    props({} as SessionRecordingDataLogicProps),
    key(({ sessionRecordingId }) => sessionRecordingId || 'no-session-recording-id'),
    connect({
        logic: [eventUsageLogic],
        values: [featureFlagLogic, ['featureFlags'], teamLogic, ['currentTeam']],
    }),
    defaults({
        sessionPlayerMetaData: null as SessionRecordingType | null,
    }),
    actions({
        setFilters: (filters: Partial<RecordingEventsFilters>) => ({ filters }),
        loadRecordingMeta: true,
        loadRecordingComments: true,
        maybeLoadRecordingMeta: true,
        loadSnapshots: true,
        loadSnapshotSources: (breakpointLength?: number) => ({ breakpointLength }),
        loadNextSnapshotSource: true,
        loadSnapshotsForSource: (source: Pick<SessionRecordingSnapshotSource, 'source' | 'blob_key'>) => ({ source }),
        loadEvents: true,
        loadFullEventData: (event: RecordingEventType | RecordingEventType[]) => ({ event }),
        markViewed: (delay?: number) => ({ delay }),
        reportUsageIfFullyLoaded: true,
        persistRecording: true,
        maybePersistRecording: true,
        pollRealtimeSnapshots: true,
        stopRealtimePolling: true,
        setTrackedWindow: (windowId: string | null) => ({ windowId }),
        setWasMarkedViewed: (wasMarkedViewed: boolean) => ({ wasMarkedViewed }),
    }),
    reducers(() => ({
        trackedWindow: [
            null as string | null,
            {
                setTrackedWindow: (_, { windowId }) => windowId,
            },
        ],
        filters: [
            {} as Partial<RecordingEventsFilters>,
            {
                setFilters: (state, { filters }) => ({ ...state, ...filters }),
            },
        ],
        isRealtimePolling: [
            false as boolean,
            {
                pollRealtimeSnapshots: () => true,
                stopRealtimePolling: () => false,
            },
        ],
        isNotFound: [
            false as boolean,
            {
                loadRecordingMeta: () => false,
                loadRecordingMetaSuccess: () => false,
                loadRecordingMetaFailure: () => true,
            },
        ],
        snapshotsBySource: [
            null as Record<string, SessionRecordingSnapshotSourceResponse> | null,
            {
                loadSnapshotsForSourceSuccess: (state, { snapshotsForSource }) => {
                    const sourceKey = getSourceKey(snapshotsForSource.source)

                    return {
                        ...state,
                        [sourceKey]: snapshotsForSource,
                    }
                },
            },
        ],
        wasMarkedViewed: [
            false as boolean,
            {
                setWasMarkedViewed: (_, { wasMarkedViewed }) => wasMarkedViewed,
            },
        ],
    })),
    loaders(({ values, props, cache }) => ({
        sessionComments: {
            loadRecordingComments: async (_, breakpoint) => {
                const empty: RecordingComment[] = []
                if (!props.sessionRecordingId) {
                    return empty
                }

                const response = await api.notebooks.recordingComments(props.sessionRecordingId)
                breakpoint()

                return response.results || empty
            },
        },
        sessionPlayerMetaData: {
            loadRecordingMeta: async (_, breakpoint) => {
                if (!props.sessionRecordingId) {
                    return null
                }

                cache.metaStartTime = performance.now()

                const response = await api.recordings.get(props.sessionRecordingId)
                breakpoint()

                return response
            },

            persistRecording: async (_, breakpoint) => {
                if (!values.sessionPlayerMetaData) {
                    return null
                }
                await breakpoint(100)
                await api.recordings.persist(props.sessionRecordingId)

                return {
                    ...values.sessionPlayerMetaData,
                    storage: 'object_storage_lts',
                }
            },
        },
        snapshotSources: [
            null as SessionRecordingSnapshotSource[] | null,
            {
                loadSnapshotSources: async ({ breakpointLength }, breakpoint) => {
                    if (breakpointLength) {
                        await breakpoint(breakpointLength)
                    }
                    const response = await api.recordings.listSnapshotSources(props.sessionRecordingId)
                    if (!response.sources) {
                        return []
                    }
                    const anyBlobV2 = response.sources.some((s) => s.source === SnapshotSourceType.blob_v2)
                    if (values.featureFlags[FEATURE_FLAGS.RECORDINGS_BLOBBY_V2_REPLAY] && anyBlobV2) {
                        return response.sources.filter((s) => s.source === SnapshotSourceType.blob_v2)
                    }
                    return response.sources.filter((s) => s.source !== SnapshotSourceType.blob_v2)
                },
            },
        ],
        snapshotsForSource: [
            null as SessionRecordingSnapshotSourceResponse | null,
            {
                loadSnapshotsForSource: async ({ source }, breakpoint) => {
                    let params: SessionRecordingSnapshotParams

                    if (source.source === SnapshotSourceType.blob) {
                        if (!source.blob_key) {
                            throw new Error('Missing key')
                        }
                        params = { blob_key: source.blob_key, source: 'blob' }
                    } else if (source.source === SnapshotSourceType.realtime) {
                        params = { source: 'realtime', version: '2024-04-30' }
                    } else if (source.source === SnapshotSourceType.blob_v2) {
                        params = { source: 'blob_v2', blob_key: source.blob_key }
                    } else {
                        throw new Error(`Unsupported source: ${source.source}`)
                    }

                    const snapshotLoadingStartTime = performance.now()

                    if (!cache.snapshotsStartTime) {
                        cache.snapshotsStartTime = snapshotLoadingStartTime
                    }

                    await breakpoint(1)

                    const response = await api.recordings.getSnapshots(props.sessionRecordingId, params).catch((e) => {
                        if (source.source === 'realtime' && e.status === 404) {
                            // Realtime source is not always available so a 404 is expected
                            return []
                        }
                        throw e
                    })

                    const { transformed, untransformed } = await processEncodedResponse(
                        response,
                        props,
                        values.featureFlags
                    )

                    return { snapshots: transformed, untransformed_snapshots: untransformed ?? undefined, source }
                },
            },
        ],
        sessionEventsData: [
            null as null | RecordingEventType[],
            {
                loadEvents: async () => {
                    if (!cache.eventsStartTime) {
                        cache.eventsStartTime = performance.now()
                    }

                    const { start, end, person } = values.sessionPlayerData

                    if (!person || !start || !end) {
                        return null
                    }

                    const sessionEventsQuery = hogql`
                            SELECT uuid, event, timestamp, elements_chain, properties.$window_id, properties.$current_url, properties.$event_type, properties.$viewport_width, properties.$viewport_height
                            FROM events
                            WHERE timestamp > ${start.subtract(TWENTY_FOUR_HOURS_IN_MS, 'ms')}
                              AND timestamp < ${end.add(TWENTY_FOUR_HOURS_IN_MS, 'ms')}
                              AND $session_id = ${props.sessionRecordingId}
                              ORDER BY timestamp ASC
                        LIMIT 1000000
                        `

                    let relatedEventsQuery = hogql`
                            SELECT uuid, event, timestamp, elements_chain, properties.$window_id, properties.$current_url, properties.$event_type
                            FROM events
                            WHERE timestamp > ${start.subtract(TWENTY_FOUR_HOURS_IN_MS, 'ms')}
                              AND timestamp < ${end.add(TWENTY_FOUR_HOURS_IN_MS, 'ms')}
                              AND (empty($session_id) OR isNull($session_id)) AND properties.$lib != 'web'
                        `
                    if (person?.uuid) {
                        relatedEventsQuery += `
                            AND person_id = '${person.uuid}'
                        `
                    }
                    if (!person?.uuid && values.sessionPlayerMetaData?.distinct_id) {
                        relatedEventsQuery += `
                            AND distinct_id = ${values.sessionPlayerMetaData.distinct_id}
                        `
                    }
                    relatedEventsQuery += `
                        ORDER BY timestamp ASC
                        LIMIT 1000000
                    `

                    const [sessionEvents, relatedEvents]: any[] = await Promise.all([
                        // make one query for all events that are part of the session
                        api.query({
                            kind: NodeKind.HogQLQuery,
                            query: sessionEventsQuery,
                        }),
                        // make a second for all events from that person,
                        // not marked as part of the session
                        // but in the same time range
                        // these are probably e.g. backend events for the session
                        // but with no session id
                        // since posthog-js must always add session id we can also
                        // take advantage of lib being materialized and further filter
                        api.query({
                            kind: NodeKind.HogQLQuery,
                            query: relatedEventsQuery,
                        }),
                    ])

                    return [...sessionEvents.results, ...relatedEvents.results].map(
                        (event: any): RecordingEventType => {
                            const currentUrl = event[5]
                            // We use the pathname to simplify the UI - we build it here instead of fetching it to keep data usage small
                            let pathname: string | undefined
                            try {
                                pathname = event[5] ? new URL(event[5]).pathname : undefined
                            } catch {
                                pathname = undefined
                            }

                            const viewportWidth = event.length > 7 ? event[7] : undefined
                            const viewportHeight = event.length > 8 ? event[8] : undefined

                            return {
                                id: event[0],
                                event: event[1],
                                timestamp: event[2],
                                elements: chainToElements(event[3]),
                                properties: {
                                    $window_id: event[4],
                                    $current_url: currentUrl,
                                    $event_type: event[6],
                                    $pathname: pathname,
                                    $viewport_width: viewportWidth,
                                    $viewport_height: viewportHeight,
                                },
                                playerTime: +dayjs(event[2]) - +start,
                                fullyLoaded: false,
                            }
                        }
                    )
                },

                loadFullEventData: async ({ event }) => {
                    // box so we're always dealing with a list
                    const events = Array.isArray(event) ? event : [event]

                    let existingEvents = values.sessionEventsData?.filter((x) => events.some((e) => e.id === x.id))

                    const allEventsAreFullyLoaded =
                        existingEvents?.every((e) => e.fullyLoaded) && existingEvents.length === events.length
                    if (!existingEvents || allEventsAreFullyLoaded) {
                        return values.sessionEventsData
                    }

                    existingEvents = existingEvents.filter((e) => !e.fullyLoaded)
                    const timestamps = existingEvents.map((ee) => dayjs(ee.timestamp).utc().valueOf())
                    const eventNames = Array.from(new Set(existingEvents.map((ee) => ee.event)))
                    const eventIds = existingEvents.map((ee) => ee.id)
                    const earliestTimestamp = timestamps.reduce((a, b) => Math.min(a, b))
                    const latestTimestamp = timestamps.reduce((a, b) => Math.max(a, b))

                    try {
                        const query: HogQLQuery = {
                            kind: NodeKind.HogQLQuery,
                            query: hogql`SELECT properties, uuid
                                         FROM events
                                        -- the timestamp range here is only to avoid querying too much of the events table
                                        -- we don't really care about the absolute value,
                                        -- but we do care about whether timezones have an odd impact
                                        -- so, we extend the range by a day on each side so that timezones don't cause issues
                                         WHERE timestamp > ${dayjs(earliestTimestamp).subtract(1, 'day')}
                                           AND timestamp < ${dayjs(latestTimestamp).add(1, 'day')}
                                           AND event in ${eventNames}
                                           AND uuid in ${eventIds}`,
                        }

                        const response = await api.query(query)
                        if (response.error) {
                            throw new Error(response.error)
                        }

                        for (const event of existingEvents) {
                            const result = response.results.find((x: any) => {
                                return x[1] === event.id
                            })

                            if (result) {
                                event.properties = JSON.parse(result[0])
                                event.fullyLoaded = true
                            }
                        }
                    } catch (e) {
                        // NOTE: This is not ideal but should happen so rarely that it is tolerable.
                        existingEvents.forEach((e) => (e.fullyLoaded = true))
                        captureException(e, {
                            tags: { feature: 'session-recording-load-full-event-data' },
                        })
                    }

                    // here we map the events list because we want the result to be a new instance to trigger downstream recalculation
                    return !values.sessionEventsData
                        ? values.sessionEventsData
                        : values.sessionEventsData.map((x) => {
                              const event = existingEvents?.find((ee) => ee.id === x.id)
                              return event
                                  ? ({
                                        ...x,
                                        properties: event.properties,
                                        fullyLoaded: event.fullyLoaded,
                                    } as RecordingEventType)
                                  : x
                          })
                },
            },
        ],
    })),
    listeners(({ values, actions, cache, props }) => ({
        loadSnapshots: () => {
            // This kicks off the loading chain
            if (!values.snapshotSourcesLoading) {
                actions.loadSnapshotSources()
            }
        },
        maybeLoadRecordingMeta: () => {
            if (!values.sessionPlayerMetaDataLoading) {
                actions.loadRecordingMeta()
            }
            if (!values.sessionCommentsLoading) {
                actions.loadRecordingComments()
            }
        },
        loadSnapshotSources: () => {
            // We only load events once we actually start loading the recording
            actions.loadEvents()
        },
        loadRecordingMetaSuccess: () => {
            cache.metadataLoadDuration = Math.round(performance.now() - cache.metaStartTime)
            actions.reportUsageIfFullyLoaded()
        },
        loadRecordingMetaFailure: () => {
            cache.metadataLoadDuration = Math.round(performance.now() - cache.metaStartTime)
        },

        loadSnapshotSourcesSuccess: () => {
            // When we receive the list of sources we can kick off the loading chain
            actions.loadNextSnapshotSource()
        },

        loadSnapshotsForSourceSuccess: ({ snapshotsForSource }) => {
            const sources = values.snapshotSources
            const snapshots = snapshotsForSource.snapshots

            // Cache the last response count to detect if we're getting the same data over and over
            const newSnapshotsCount = snapshots.length

            if ((cache.lastSnapshotsCount ?? newSnapshotsCount) === newSnapshotsCount) {
                // if we're getting no results from realtime polling we can increment faster
                // so that we stop polling sooner
                const increment = newSnapshotsCount === 0 ? 2 : 1
                cache.lastSnapshotsUnchangedCount = (cache.lastSnapshotsUnchangedCount ?? 0) + increment
            } else {
                cache.lastSnapshotsUnchangedCount = 0
            }
            cache.lastSnapshotsCount = newSnapshotsCount

            if (!snapshots.length && sources?.length === 1) {
                // We got only a single source to load, loaded it successfully, but it had no snapshots.
                posthog.capture('recording_snapshots_v2_empty_response', {
                    source: sources[0],
                })
            } else if (!cache.firstPaintDuration) {
                cache.firstPaintDuration = Math.round(performance.now() - cache.snapshotsStartTime)
            }
            if (!values.wasMarkedViewed) {
                actions.markViewed()
            }

            actions.loadNextSnapshotSource()
        },

        loadNextSnapshotSource: () => {
            const nextSourceToLoad = values.snapshotSources?.find((s) => {
                const sourceKey = getSourceKey(s)
                return !values.snapshotsBySource?.[sourceKey]
            })

            if (nextSourceToLoad) {
                return actions.loadSnapshotsForSource(nextSourceToLoad)
            }

            if (values.snapshotSources?.find((s) => s.source === SnapshotSourceType.blob_v2)) {
                actions.loadSnapshotSources(DEFAULT_V2_POLLING_INTERVAL_MS)
            }

            // TODO: Move this to a one time check - only report once per recording
            cache.snapshotsLoadDuration = Math.round(performance.now() - cache.snapshotsStartTime)
            actions.reportUsageIfFullyLoaded()

            // If we have a realtime source, start polling it
            const realTimeSource = values.snapshotSources?.find((s) => s.source === SnapshotSourceType.realtime)
            if (realTimeSource) {
                actions.pollRealtimeSnapshots()
            }
        },
        loadSnapshotsForSourceFailure: () => {
            cache.snapshotsLoadDuration = Math.round(performance.now() - cache.snapshotsStartTime)
        },
        pollRealtimeSnapshots: () => {
            // always make sure we've cleared up the last timeout
            clearTimeout(cache.realTimePollingTimeoutID)
            cache.realTimePollingTimeoutID = null

            // ten is an arbitrary limit to try to avoid sending requests to our backend unnecessarily
            // we could change this or add to it e.g. only poll if browser is visible to user
            if ((cache.lastSnapshotsUnchangedCount ?? 0) <= 10) {
                cache.realTimePollingTimeoutID = setTimeout(() => {
                    actions.loadSnapshotsForSource({ source: SnapshotSourceType.realtime })
                }, props.realTimePollingIntervalMilliseconds || DEFAULT_REALTIME_POLLING_MILLIS)
            } else {
                actions.stopRealtimePolling()
            }
        },
        loadEventsSuccess: () => {
            cache.eventsLoadDuration = Math.round(performance.now() - cache.eventsStartTime)
            actions.reportUsageIfFullyLoaded()
        },
        loadEventsFailure: () => {
            cache.eventsLoadDuration = Math.round(performance.now() - cache.eventsStartTime)
        },
        reportUsageIfFullyLoaded: (_, breakpoint) => {
            breakpoint()
            if (values.fullyLoaded) {
                eventUsageLogic.actions.reportRecording(
                    values.sessionPlayerData,
                    generateRecordingReportDurations(cache),
                    SessionRecordingUsageType.LOADED,
                    values.sessionPlayerMetaData,
                    0
                )
                // Reset cache now that final usage report has been sent
                resetTimingsCache(cache)
            }
        },
        markViewed: async ({ delay }, breakpoint) => {
            const durations = generateRecordingReportDurations(cache)
            // Triggered on first paint
            breakpoint()
            if (values.wasMarkedViewed) {
                return
            }
            actions.setWasMarkedViewed(true) // this prevents us from calling the function multiple times

            await breakpoint(IS_TEST_MODE ? 1 : delay ?? 3000)
            await api.recordings.update(props.sessionRecordingId, {
                viewed: true,
                player_metadata: values.sessionPlayerMetaData,
                durations,
            })
            await breakpoint(IS_TEST_MODE ? 1 : 10000)
            await api.recordings.update(props.sessionRecordingId, {
                analyzed: true,
                player_metadata: values.sessionPlayerMetaData,
                durations,
            })
        },

        maybePersistRecording: () => {
            if (values.sessionPlayerMetaDataLoading) {
                return
            }

            if (values.sessionPlayerMetaData?.storage === 'object_storage') {
                actions.persistRecording()
            }
        },
    })),
    selectors(({ cache }) => ({
        webVitalsEvents: [
            (s) => [s.sessionEventsData],
            (sessionEventsData): RecordingEventType[] =>
                (sessionEventsData || []).filter((e) => e.event === '$web_vitals'),
        ],
        AIEvents: [
            (s) => [s.sessionEventsData],
            (sessionEventsData): RecordingEventType[] =>
                // see if event start with $ai_
                (sessionEventsData || []).filter((e) => e.event.startsWith('$ai_')),
        ],
        windowIdForTimestamp: [
            (s) => [s.segments],
            (segments) =>
                (timestamp: number): string | undefined => {
                    return segments.find(
                        (segment) => segment.startTimestamp <= timestamp && segment.endTimestamp >= timestamp
                    )?.windowId
                },
        ],
        eventViewports: [
            (s) => [s.sessionEventsData],
            (sessionEventsData): (ViewportResolution & { timestamp: string | number })[] =>
                (sessionEventsData || [])
                    .filter((e) => e.properties.$viewport_width && e.properties.$viewport_height)
                    .map((e) => ({
                        width: e.properties.$viewport_width,
                        height: e.properties.$viewport_height,
                        href: e.properties.$current_url,
                        timestamp: e.timestamp,
                    })),
        ],
        viewportForTimestamp: [
            (s) => [s.eventViewports],
            (eventViewports) =>
                (timestamp: number): ViewportResolution | undefined => {
                    // we do this as a function because in most recordings we don't need the data so we don't need to run this every time

                    // First try to find the first event after the timestamp that has viewport dimensions
                    const nextEvent = eventViewports
                        .filter((e) => dayjs(e.timestamp).isSameOrAfter(dayjs(timestamp)))
                        .sort((a, b) => dayjs(a.timestamp).valueOf() - dayjs(b.timestamp).valueOf())[0]

                    if (nextEvent) {
                        return {
                            width: nextEvent.width,
                            height: nextEvent.height,
                            href: nextEvent.href,
                        }
                    }

                    // If no event after timestamp, find the closest event before it
                    const previousEvent = eventViewports
                        .filter((e) => dayjs(e.timestamp).isBefore(dayjs(timestamp)))
                        .sort((a, b) => dayjs(b.timestamp).valueOf() - dayjs(a.timestamp).valueOf())[0] // Sort descending to get closest

                    if (previousEvent) {
                        return {
                            width: previousEvent.width,
                            height: previousEvent.height,
                            href: previousEvent.href,
                        }
                    }

                    return undefined
                },
        ],
        sessionPlayerData: [
            (s, p) => [
                s.sessionPlayerMetaData,
                s.snapshotsByWindowId,
                s.segments,
                s.bufferedToTime,
                s.start,
                s.end,
                s.durationMs,
                s.fullyLoaded,
                p.sessionRecordingId,
            ],
            (
                meta,
                snapshotsByWindowId,
                segments,
                bufferedToTime,
                start,
                end,
                durationMs,
                fullyLoaded,
                sessionRecordingId
            ): SessionPlayerData => ({
                person: meta?.person ?? null,
                start,
                end,
                durationMs,
                snapshotsByWindowId,
                segments,
                bufferedToTime,
                fullyLoaded,
                sessionRecordingId,
            }),
        ],

        snapshotsLoading: [
            (s) => [s.snapshotSourcesLoading, s.snapshotsForSourceLoading, s.snapshots, s.featureFlags],
            (
                snapshotSourcesLoading: boolean,
                snapshotsForSourceLoading: boolean,
                snapshots: RecordingSnapshot[],
                featureFlags: FeatureFlagsSet
            ): boolean => {
                // For v2 recordings, only show loading if we have no snapshots yet
                if (featureFlags[FEATURE_FLAGS.RECORDINGS_BLOBBY_V2_REPLAY]) {
                    return snapshots.length === 0
                }

                // Default behavior for non-v2 recordings
                // if there's a realTimePollingTimeoutID, don't signal that we're loading
                // we don't want the UI to flip to "loading" every time we poll
                return !cache.realTimePollingTimeoutID && (snapshotSourcesLoading || snapshotsForSourceLoading)
            },
        ],

        snapshotsLoaded: [(s) => [s.snapshotSources], (snapshotSources): boolean => !!snapshotSources],

        fullyLoaded: [
            (s) => [s.snapshots, s.sessionPlayerMetaDataLoading, s.snapshotsLoading, s.sessionEventsDataLoading],
            (snapshots, sessionPlayerMetaDataLoading, snapshotsLoading, sessionEventsDataLoading): boolean => {
                // TODO: Do a proper check for all sources having been loaded
                return (
                    !!snapshots.length &&
                    !sessionPlayerMetaDataLoading &&
                    !snapshotsLoading &&
                    !sessionEventsDataLoading
                )
            },
        ],

        firstSnapshot: [
            (s) => [s.snapshots],
            (snapshots): RecordingSnapshot | null => {
                return snapshots[0] || null
            },
        ],

        lastSnapshot: [
            (s) => [s.snapshots],
            (snapshots): RecordingSnapshot | null => {
                return snapshots[snapshots.length - 1] || null
            },
        ],

        start: [
            (s) => [s.firstSnapshot, s.sessionPlayerMetaData],
            (firstSnapshot, meta): Dayjs | null => {
                const eventStart = meta?.start_time ? dayjs(meta.start_time) : null
                const snapshotStart = firstSnapshot ? dayjs(firstSnapshot.timestamp) : null

                // whichever is earliest
                if (eventStart && snapshotStart) {
                    return eventStart.isBefore(snapshotStart) ? eventStart : snapshotStart
                }
                return eventStart || snapshotStart
            },
        ],

        end: [
            (s) => [s.lastSnapshot, s.sessionPlayerMetaData],
            (lastSnapshot, meta): Dayjs | null => {
                const eventEnd = meta?.end_time ? dayjs(meta.end_time) : null
                const snapshotEnd = lastSnapshot ? dayjs(lastSnapshot.timestamp) : null

                // whichever is latest
                if (eventEnd && snapshotEnd) {
                    return eventEnd.isAfter(snapshotEnd) ? eventEnd : snapshotEnd
                }
                return eventEnd || snapshotEnd
            },
        ],

        durationMs: [
            (s) => [s.start, s.end],
            (start, end): number => {
                return !!start && !!end ? end.diff(start) : 0
            },
        ],

        segments: [
            (s) => [s.snapshots, s.start, s.end, s.trackedWindow],
            (snapshots, start, end, trackedWindow): RecordingSegment[] => {
                return createSegments(snapshots || [], start, end, trackedWindow)
            },
        ],

        urls: [
            (s) => [s.snapshots],
            (snapshots): { url: string; timestamp: number }[] => {
                return (
                    snapshots
                        .filter((snapshot) => getHrefFromSnapshot(snapshot))
                        .map((snapshot) => {
                            return {
                                url: getHrefFromSnapshot(snapshot) as string,
                                timestamp: snapshot.timestamp,
                            }
                        }) ?? []
                )
            },
        ],

        snapshots: [
            (s, p) => [s.snapshotSources, s.snapshotsBySource, s.viewportForTimestamp, p.sessionRecordingId],
            (sources, snapshotsBySource, viewportForTimestamp, sessionRecordingId): RecordingSnapshot[] => {
                const allSnapshots =
                    sources?.flatMap((source) => {
                        const sourceKey = getSourceKey(source)
                        return snapshotsBySource?.[sourceKey]?.snapshots || []
                    }) ?? []

                return patchMetaEventIntoWebData(
                    deduplicateSnapshots(allSnapshots),
                    viewportForTimestamp,
                    sessionRecordingId
                )
            },
        ],

        untransformedSnapshots: [
            (s) => [s.snapshotSources, s.snapshotsBySource],
            (sources, snapshotsBySource): RecordingSnapshot[] => {
                const allSnapshots =
                    sources?.flatMap((source) => {
                        const sourceKey = getSourceKey(source)
                        return snapshotsBySource?.[sourceKey]?.untransformed_snapshots || []
                    }) ?? []

                return deduplicateSnapshots(allSnapshots)
            },
        ],

        snapshotsByWindowId: [
            (s) => [s.snapshots],
            (snapshots): Record<string, eventWithTime[]> => {
                return mapSnapshotsToWindowId(snapshots || [])
            },
        ],

        snapshotsInvalid: [
            (s, p) => [s.snapshotsByWindowId, s.fullyLoaded, s.start, p.sessionRecordingId, s.currentTeam],
            (snapshotsByWindowId, fullyLoaded, start, sessionRecordingId, currentTeam): boolean => {
                if (!fullyLoaded || !start) {
                    return false
                }

                const windowsHaveFullSnapshot = Object.entries(snapshotsByWindowId).reduce(
                    (acc, [windowId, events]) => {
                        acc[`window-id-${windowId}-has-full-snapshot`] = events.some(
                            (event) => event.type === EventType.FullSnapshot
                        )
                        return acc
                    },
                    {}
                )
                const anyWindowMissingFullSnapshot = !Object.values(windowsHaveFullSnapshot).some((x) => x)
                const everyWindowMissingFullSnapshot = !Object.values(windowsHaveFullSnapshot).every((x) => x)

                if (everyWindowMissingFullSnapshot) {
                    // video is definitely unplayable
                    posthog.capture('recording_has_no_full_snapshot', {
                        watchedSession: sessionRecordingId,
                        teamId: currentTeam?.id,
                        teamName: currentTeam?.name,
                    })
                } else if (anyWindowMissingFullSnapshot) {
                    posthog.capture('recording_window_missing_full_snapshot', {
                        watchedSession: sessionRecordingId,
                        teamID: currentTeam?.id,
                        teamName: currentTeam?.name,
                    })
                }

                return everyWindowMissingFullSnapshot
            },
        ],

        isRecentAndInvalid: [
            (s) => [s.start, s.snapshotsInvalid],
            (start, snapshotsInvalid) => {
                const lessThanFiveMinutesOld = dayjs().diff(start, 'minute') <= 5
                return snapshotsInvalid && lessThanFiveMinutesOld
            },
        ],

        bufferedToTime: [
            (s) => [s.segments],
            (segments): number | null => {
                if (!segments.length) {
                    return null
                }

                const startTime = segments[0].startTimestamp
                const lastSegment = segments[segments.length - 1]

                if (lastSegment.kind === 'buffer') {
                    return lastSegment.startTimestamp - startTime
                }

                return lastSegment.endTimestamp - startTime
            },
        ],

        windowIds: [
            (s) => [s.snapshotsByWindowId],
            (snapshotsByWindowId) => {
                return Object.keys(snapshotsByWindowId)
            },
        ],

        createExportJSON: [
            (s) => [s.sessionPlayerMetaData, s.snapshots, s.untransformedSnapshots],
            (
                sessionPlayerMetaData,
                snapshots,
                untransformedSnapshots
            ): ((exportUntransformedMobileSnapshotData: boolean) => ExportedSessionRecordingFileV2) => {
                return (exportUntransformedMobileSnapshotData: boolean) => ({
                    version: '2023-04-28',
                    data: {
                        id: sessionPlayerMetaData?.id ?? '',
                        person: sessionPlayerMetaData?.person,
                        snapshots: exportUntransformedMobileSnapshotData ? untransformedSnapshots : snapshots,
                    },
                })
            },
        ],

        customRRWebEvents: [
            (s) => [s.snapshots],
            (snapshots): customEvent[] => {
                return snapshots.filter((snapshot) => snapshot.type === EventType.Custom).map((x) => x as customEvent)
            },
        ],
    })),
    subscriptions(({ actions, values }) => ({
        webVitalsEvents: (value: RecordingEventType[]) => {
            // we preload all web vitals data, so it can be used before user interaction
            if (!values.sessionEventsDataLoading) {
                actions.loadFullEventData(value)
            }
        },
        AIEvents: (value: RecordingEventType[]) => {
            // we preload all AI  data, so it can be used before user interaction
            if (value.length > 0) {
                actions.loadFullEventData(value)
            }
        },
        isRecentAndInvalid: (prev: boolean, next: boolean) => {
            if (!prev && next) {
                posthog.capture('recording cannot playback yet', {
                    watchedSession: values.sessionPlayerData.sessionRecordingId,
                })
            }
        },
    })),
    afterMount(({ cache }) => {
        resetTimingsCache(cache)
    }),
    beforeUnmount(({ cache }) => {
        resetTimingsCache(cache)
    }),
])
