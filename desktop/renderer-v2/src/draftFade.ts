export const proseFadeDuration = 240;

type PaintRange = { start: number; end: number; at: number };
export type DraftPaint = { source: string; ranges: PaintRange[]; now: number };

export function recordDraftPaint(previous: DraftPaint, source: string, fading: boolean, now: number): DraftPaint {
  if (!fading) return { source, ranges: [], now };
  let prefixLength = Math.min(source.length, previous.source.length);
  if (!source.startsWith(previous.source.slice(0, prefixLength))) {
    prefixLength = 0;
    while (prefixLength < source.length && source[prefixLength] === previous.source[prefixLength]) {
      prefixLength++;
    }
  }
  const ranges = previous.ranges
    .filter(range => range.start < prefixLength && now - range.at < proseFadeDuration)
    .map(range => range.end <= prefixLength ? range : {...range, end:prefixLength});
  if (prefixLength < source.length) ranges.push({ start:prefixLength, end:source.length, at:now });
  return { source, ranges, now };
}

export function draftGlyphAge(paint: DraftPaint | undefined, offset: number): number {
  const range = paint?.ranges.find(range => range.start <= offset && offset < range.end);
  return paint && range ? paint.now - range.at : proseFadeDuration;
}
