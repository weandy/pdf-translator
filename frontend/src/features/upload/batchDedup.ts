interface Candidate {
  state: "ready" | "uploading" | "done";
  pages: number | null;
  checking: boolean;
  seen: unknown;
  confirmed: boolean;
}

type ErrorFor<T> = (item: T) => string | null;

export function isPendingDuplicate<T extends Candidate>(item: T, itemError: ErrorFor<T>): boolean {
  return item.state === "ready" && !itemError(item) && !item.checking && Boolean(item.seen && !item.confirmed);
}

export function confirmPendingDuplicates<T extends Candidate>(items: T[], itemError: ErrorFor<T>): T[] {
  return items.map((item) => isPendingDuplicate(item, itemError) ? { ...item, confirmed: true } : item);
}

export function removePendingDuplicates<T extends Candidate>(
  selection: { items: T[]; rangeText: string; pickSeq: number },
  itemError: ErrorFor<T>,
) {
  const items = selection.items.filter((item) => !isPendingDuplicate(item, itemError));
  return { ...selection, items, rangeText: items.length <= 1 ? "" : selection.rangeText };
}

export function allFilesChecked(items: Candidate[]): boolean {
  return items.every((item) => !item.checking && item.pages !== null);
}

export function uploadQueue<T extends Candidate>(items: T[], itemError: ErrorFor<T>): T[] {
  return items.filter((item) => item.state === "ready" && !itemError(item) &&
    item.pages !== null && !item.checking && (!item.seen || item.confirmed));
}
