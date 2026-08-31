/**
 * An incremental Server-Sent Events parser over a `fetch` body stream.
 *
 * `EventSource` is unusable here: it cannot POST, cannot be aborted cleanly and
 * is not available in an MV3 service worker. So the background reads the raw
 * `ReadableStream` and frames the messages itself, following the HTML spec's
 * event-stream algorithm closely enough that a real SSE server — ours or a
 * proxy in front of it — cannot surprise it.
 *
 * What that means concretely, and what tests/sse.test.ts pins down:
 *
 *  - bytes arrive in arbitrary chunks, so a field, a line, or even a single
 *    UTF-8 code point may be split across reads (`TextDecoder` with
 *    `{ stream: true }` handles the last case);
 *  - a chunk may carry several complete messages at once;
 *  - lines end with `\n`, `\r\n` or a bare `\r`, and a trailing `\r` at a chunk
 *    boundary must not be mistaken for a line break until the next byte proves
 *    it is not the start of a `\r\n`;
 *  - a line starting with `:` is a comment — our backend sends `: keep-alive`
 *    every 20 s so the service worker's fetch is never idle-killed — and must
 *    never surface as an event;
 *  - repeated `data:` lines are joined with `\n`;
 *  - a message with no `event:` line defaults to the name `message`;
 *  - a final message that the server did not terminate with a blank line is
 *    still delivered when the stream ends.
 *
 * One deliberate deviation from the spec: `id` is reported only for the message
 * that actually carried an `id:` line, instead of persisting as a sticky
 * "last event ID" across later messages. Re-Vera uses the id purely as the
 * job's monotonic sequence number for dropping already-applied events, so
 * inheriting a previous message's id would be actively misleading. Every
 * message our backend sends carries its own id.
 */

/** One framed SSE message. `data` is the raw payload text, not parsed JSON. */
export interface SSEMessage {
  /** The `id:` field of this message, if it had one. Our backend sends a sequence number. */
  id?: string
  /** The `event:` field, or `"message"` when the server omitted it. */
  event: string
  /** All `data:` lines of the message, joined with `\n`. */
  data: string
}

/** SSE's default event name when the server sends no `event:` field. */
const DEFAULT_EVENT = 'message'

/**
 * Frame a `fetch` response body into SSE messages.
 *
 * Yields each message as it completes. Returns when the server closes the
 * stream; throws whatever the underlying reader throws, so an `AbortSignal`
 * firing on the originating `fetch` surfaces here as an `AbortError`. Leaving
 * the loop early (`break`, `return`, or an exception) cancels the body, which
 * releases the connection.
 */
export async function* parseSSE(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SSEMessage> {
  const reader = stream.getReader()
  // The default decoder strips a leading BOM for us and, with { stream: true },
  // holds back an incomplete multi-byte sequence until the next chunk.
  const decoder = new TextDecoder()

  /** Text read but not yet split into complete lines. */
  let buffer = ''

  /* Fields accumulated for the message currently being built. */
  let eventName: string | null = null
  let eventId: string | undefined
  let dataLines: string[] = []

  function reset(): void {
    eventName = null
    eventId = undefined
    dataLines = []
  }

  /**
   * End the current message. Returns null when there was nothing to dispatch —
   * a blank line after only comments, or two blank lines in a row.
   */
  function dispatch(): SSEMessage | null {
    if (dataLines.length === 0) {
      // Per spec: no data field means no event, but any `event:`/`id:` seen so
      // far is still discarded.
      reset()
      return null
    }
    const message: SSEMessage = {
      event: eventName ?? DEFAULT_EVENT,
      data: dataLines.join('\n'),
    }
    if (eventId !== undefined) message.id = eventId
    reset()
    return message
  }

  /** Apply one complete line. Returns a message when the line completed one. */
  function consume(line: string): SSEMessage | null {
    if (line === '') return dispatch()
    if (line.startsWith(':')) return null // comment, e.g. ": keep-alive"

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    // Exactly one optional space after the colon belongs to the framing.
    if (value.startsWith(' ')) value = value.slice(1)

    switch (field) {
      case 'event':
        eventName = value
        break
      case 'data':
        dataLines.push(value)
        break
      case 'id':
        // The spec ignores an id containing U+0000.
        if (!value.includes('\0')) eventId = value
        break
      case 'retry':
        // Reconnection backoff is the server's advice to EventSource; the
        // background owns its own retry policy, so this is ignored.
        break
      default:
        // Unknown fields are ignored, which is what makes SSE extensible.
        break
    }
    return null
  }

  /**
   * Pull every complete line out of `buffer`, leaving the unterminated
   * remainder behind. With `flush`, the remainder counts as a final line —
   * that is what delivers a last message the server did not blank-line.
   */
  function* takeLines(flush: boolean): Generator<string> {
    let index = 0
    while (index < buffer.length) {
      const cr = buffer.indexOf('\r', index)
      const lf = buffer.indexOf('\n', index)
      let breakAt: number
      let skip: number

      if (cr !== -1 && (lf === -1 || cr < lf)) {
        // A `\r` in the last position may yet turn out to be a `\r\n`; wait for
        // the next chunk before deciding, unless there will not be one.
        if (cr === buffer.length - 1 && !flush) break
        breakAt = cr
        skip = buffer[cr + 1] === '\n' ? 2 : 1
      } else if (lf !== -1) {
        breakAt = lf
        skip = 1
      } else {
        break
      }

      yield buffer.slice(index, breakAt)
      index = breakAt + skip
    }

    buffer = buffer.slice(index)
    if (flush && buffer.length > 0) {
      const remainder = buffer
      buffer = ''
      yield remainder
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      for (const line of takeLines(false)) {
        const message = consume(line)
        if (message) yield message
      }
    }

    // Flush the decoder (a truncated multi-byte tail becomes U+FFFD), then the
    // buffer, then any message left open by a missing blank line.
    buffer += decoder.decode()
    for (const line of takeLines(true)) {
      const message = consume(line)
      if (message) yield message
    }
    const trailing = dispatch()
    if (trailing) yield trailing
  } finally {
    // Runs on normal end, on `break` from the consumer, and on error. Cancelling
    // a stream that already ended is a no-op; cancelling a live one releases the
    // connection instead of leaking it until the worker dies.
    await reader.cancel().catch(() => undefined)
  }
}
