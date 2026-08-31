import { describe, expect, it } from 'vitest'

import { parseSSE, type SSEMessage } from '../src/background/sse'

/**
 * The SSE framing is the one place where the extension and the backend can
 * silently disagree: everything still "works" until a keep-alive lands mid-JSON
 * or a claim event straddles two TCP reads, and then a claim quietly vanishes.
 * These tests feed the parser hand-cut chunks that reproduce each of those
 * shapes.
 */

const encoder = new TextEncoder()

/** A stream that delivers exactly these chunks, in this order, then closes. */
function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

/** A stream over raw bytes, for splitting a UTF-8 sequence mid-code-point. */
function byteStreamOf(...chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<SSEMessage[]> {
  const messages: SSEMessage[] = []
  for await (const message of parseSSE(stream)) messages.push(message)
  return messages
}

describe('parseSSE', () => {
  it('reassembles one event split across three chunks mid-field', async () => {
    const messages = await collect(
      streamOf('id: 1\neve', 'nt: claims_fo', 'und\ndata: {"type":"claims_found","count":6}\n\n'),
    )

    expect(messages).toEqual([
      { id: '1', event: 'claims_found', data: '{"type":"claims_found","count":6}' },
    ])
  })

  it('splits a single chunk holding two complete events', async () => {
    const messages = await collect(
      streamOf(
        'id: 1\nevent: claims_found\ndata: {"count":6}\n\n' +
          'id: 2\nevent: claim\ndata: {"id":"c3"}\n\n',
      ),
    )

    expect(messages).toEqual([
      { id: '1', event: 'claims_found', data: '{"count":6}' },
      { id: '2', event: 'claim', data: '{"id":"c3"}' },
    ])
  })

  it('ignores ": keep-alive" comments interleaved between and inside events', async () => {
    const messages = await collect(
      streamOf(
        ': keep-alive\n\n',
        'id: 1\n',
        ': keep-alive\n\n',
        'event: claim\ndata: {"id":"c1"}\n\n',
        ': keep-alive\n\n',
        'id: 2\nevent: done\ndata: {"type":"done"}\n\n',
      ),
    )

    // The comment between `id: 1` and the rest of the event is followed by a
    // blank line, which ends a message that carried no data — nothing is
    // dispatched, and the pending id is dropped with it.
    expect(messages).toEqual([
      { event: 'claim', data: '{"id":"c1"}' },
      { id: '2', event: 'done', data: '{"type":"done"}' },
    ])
  })

  it('accepts CRLF line endings, including a CRLF split across chunks', async () => {
    const messages = await collect(
      streamOf(
        'id: 7\r\nevent: claim\r\ndata: {"id":"c6"}\r',
        '\r\n',
        'id: 8\r\nevent: done\r\ndata: {"type":"done"}\r\n\r\n',
      ),
    )

    expect(messages).toEqual([
      { id: '7', event: 'claim', data: '{"id":"c6"}' },
      { id: '8', event: 'done', data: '{"type":"done"}' },
    ])
  })

  it('accepts a bare CR as a line terminator', async () => {
    const messages = await collect(streamOf('event: claim\rdata: {"id":"c2"}\r\r'))

    expect(messages).toEqual([{ event: 'claim', data: '{"id":"c2"}' }])
  })

  it('joins repeated data lines with a newline', async () => {
    const messages = await collect(
      streamOf('id: 4\nevent: note\ndata: first line\ndata: second line\ndata: third\n\n'),
    )

    expect(messages).toEqual([
      { id: '4', event: 'note', data: 'first line\nsecond line\nthird' },
    ])
  })

  it('delivers a final event that has no trailing blank line', async () => {
    const messages = await collect(
      streamOf('id: 8\nevent: done\ndata: {"type":"done","counts":{}}'),
    )

    expect(messages).toEqual([
      { id: '8', event: 'done', data: '{"type":"done","counts":{}}' },
    ])
  })

  it('defaults the event name to "message" when there is no event field', async () => {
    const messages = await collect(streamOf('data: bare payload\n\n'))

    expect(messages).toEqual([{ event: 'message', data: 'bare payload' }])
  })

  it('strips exactly one space after the colon and keeps the rest', async () => {
    const messages = await collect(streamOf('event:claim\ndata:  padded \n\n'))

    expect(messages).toEqual([{ event: 'claim', data: ' padded ' }])
  })

  it('dispatches nothing for blank lines, comment-only blocks or unknown fields', async () => {
    const messages = await collect(
      streamOf('\n\n: just a comment\n\nfoo: bar\n\nretry: 3000\n\nevent: claim\n\n'),
    )

    expect(messages).toEqual([])
  })

  it('emits an event whose data field is present but empty', async () => {
    const messages = await collect(streamOf('event: ping\ndata:\n\n'))

    expect(messages).toEqual([{ event: 'ping', data: '' }])
  })

  it('reassembles a multi-byte character split across a chunk boundary', async () => {
    // The fixture article is deliberately non-ASCII: an em dash in the dateline
    // and "·" inside trail notes. Both are three UTF-8 bytes, so a chunk
    // boundary can land inside one.
    const payload = encoder.encode('event: claim\ndata: SINGAPORE — CNA · Reuters\n\n')
    const cut = payload.indexOf(0xe2) + 1 // mid "—"

    const messages = await collect(
      byteStreamOf(payload.slice(0, cut), payload.slice(cut)),
    )

    expect(messages).toEqual([{ event: 'claim', data: 'SINGAPORE — CNA · Reuters' }])
  })

  it('parses a realistic backend transcript byte-for-byte in one-byte chunks', async () => {
    const transcript =
      'id: 1\nevent: claims_found\ndata: {"type":"claims_found","count":2}\n\n' +
      ': keep-alive\n\n' +
      'id: 2\nevent: claim\ndata: {"id":"c3","verdict":"supported"}\n\n' +
      'id: 3\nevent: claim\ndata: {"id":"c1","verdict":"contradicted"}\n\n' +
      'id: 4\nevent: done\ndata: {"type":"done","checked_at":"2026-08-31T00:00:00Z"}\n\n'

    // One character per chunk is the worst case the parser will ever see.
    const messages = await collect(streamOf(...transcript.split('')))

    expect(messages.map((m) => [m.id, m.event])).toEqual([
      ['1', 'claims_found'],
      ['2', 'claim'],
      ['3', 'claim'],
      ['4', 'done'],
    ])
    expect(JSON.parse(messages[1].data)).toEqual({ id: 'c3', verdict: 'supported' })
  })

  it('cancels the body when the consumer stops early', async () => {
    let cancelled = false
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: claim\ndata: one\n\nevent: claim\ndata: two\n\n'))
      },
      cancel() {
        cancelled = true
      },
    })

    for await (const message of parseSSE(stream)) {
      expect(message.data).toBe('one')
      break
    }

    expect(cancelled).toBe(true)
  })
})
