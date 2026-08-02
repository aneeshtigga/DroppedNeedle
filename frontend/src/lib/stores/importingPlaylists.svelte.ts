import { SvelteSet } from 'svelte/reactivity';

// Playlist ids whose Spotify/CSV import is still resolving (linking + cover art). Drives
// the cover loader on cards and the detail hero. Backed by a reactive SvelteSet so
// `has()` re-renders subscribers as ids are added/removed.
const importing = new SvelteSet<string>();

// A missed "done" SSE event must not strand a permanent spinner: every add arms a safety
// timer that force-clears the id after a while. The timer is cancelled on an explicit
// remove (the normal done-event path).
const SAFETY_MS = 5 * 60 * 1000;
const timers = new Map<string, ReturnType<typeof setTimeout>>();

function clearTimer(id: string): void {
	const t = timers.get(id);
	if (t) {
		clearTimeout(t);
		timers.delete(id);
	}
}

function has(id: string): boolean {
	return importing.has(id);
}

function add(id: string): void {
	if (!id) return;
	importing.add(id);
	clearTimer(id);
	timers.set(
		id,
		setTimeout(() => remove(id), SAFETY_MS)
	);
}

function remove(id: string): void {
	if (!id) return;
	importing.delete(id);
	clearTimer(id);
}

export const importingPlaylists = { has, add, remove };
