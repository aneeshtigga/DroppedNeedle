<script lang="ts">
	import { onDestroy } from 'svelte';
	import { Download, Check } from 'lucide-svelte';
	import { getApiUrl } from '$lib/api/api-utils';
	import { toastStore } from '$lib/stores/toast';
	import type { PlaylistTrack } from '$lib/api/playlists';

	interface Props {
		playlistId: string;
		track: PlaylistTrack;
	}

	let { playlistId, track }: Props = $props();

	interface DownloadListItem {
		id: string;
		status?: string;
		progress_percent?: number;
	}

	type Phase = 'idle' | 'loading' | 'polling' | 'done' | 'gone';

	let phase = $state<Phase>('idle');
	let progress = $state(0);
	let title = $state('Request this track (slskd)');

	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let linkTimer: ReturnType<typeof setTimeout> | null = null;
	let removeTimer: ReturnType<typeof setTimeout> | null = null;
	let cancelled = false;

	const trackLabel = $derived(track.track_name || 'track');

	function clearTimers() {
		if (pollTimer) clearTimeout(pollTimer);
		if (linkTimer) clearTimeout(linkTimer);
		if (removeTimer) clearTimeout(removeTimer);
		pollTimer = linkTimer = removeTimer = null;
	}

	onDestroy(() => {
		cancelled = true;
		clearTimers();
	});

	// A completed download may have imported files the library hasn't linked yet - ping
	// check-local now and once more shortly after so the row flips Unknown -> Local.
	function reconcileLocal() {
		const url = getApiUrl(`/api/v1/me/spotify/check-local/${playlistId}`);
		void fetch(url, { method: 'POST', credentials: 'include' }).catch(() => {});
		linkTimer = setTimeout(() => {
			void fetch(url, { method: 'POST', credentials: 'include' }).catch(() => {});
		}, 4000);
	}

	function finishSuccess() {
		phase = 'done';
		title = 'Downloaded';
		reconcileLocal();
		// Remove the button once the track is in the library.
		removeTimer = setTimeout(() => {
			if (!cancelled) phase = 'gone';
		}, 6000);
	}

	function reset() {
		clearTimers();
		phase = 'idle';
		progress = 0;
		title = 'Request this track (slskd)';
	}

	function startPolling(taskId: string) {
		phase = 'polling';
		progress = 0;
		title = 'queued';
		let seen = false;
		let iterations = 0;

		const poll = async () => {
			if (cancelled) return;
			if (++iterations > 720) return; // ~24 min cap
			try {
				const res = await fetch(getApiUrl('/api/v1/downloads?page=1&page_size=100'), {
					credentials: 'include'
				});
				const data = (await res.json()) as { items?: DownloadListItem[] };
				const item = (data.items ?? []).find((it) => it.id === taskId) ?? null;
				if (!item) {
					// The task dropped out of the list: if we saw it before it has finished
					// and been cleared; otherwise keep waiting for it to appear.
					if (seen) finishSuccess();
					else pollTimer = setTimeout(() => void poll(), 2000);
					return;
				}
				seen = true;
				const status = (item.status ?? '').toLowerCase();
				const pct = Math.round(item.progress_percent ?? 0);
				if (status.includes('complet') || status.includes('succe') || status.includes('import')) {
					finishSuccess();
					return;
				}
				if (status.includes('fail') || status.includes('cancel')) {
					reset();
					return;
				}
				progress = pct;
				title = `${status} ${pct}%`;
				pollTimer = setTimeout(() => void poll(), 2000);
			} catch {
				pollTimer = setTimeout(() => void poll(), 3000);
			}
		};

		void poll();
	}

	async function request() {
		if (phase !== 'idle') return;
		phase = 'loading';
		try {
			const res = await fetch(
				getApiUrl(`/api/v1/me/spotify/request-track/${playlistId}/${track.id}`),
				{ method: 'POST', credentials: 'include' }
			);
			if (!res.ok) throw new Error();
			const json = (await res.json()) as { status?: string; task_id?: string | null };

			// No MusicBrainz recording match: fall back to a direct Soulseek grab.
			if (!json || json.status === 'no_match' || !json.task_id) {
				toastStore.show({ message: `Searching Soulseek for "${trackLabel}"...`, type: 'info' });
				const grabRes = await fetch(
					getApiUrl(`/api/v1/me/spotify/grab-track/${playlistId}/${track.id}`),
					{ method: 'POST', credentials: 'include' }
				);
				if (!grabRes.ok) throw new Error();
				const grab = (await grabRes.json()) as { status?: string; task_id?: string | null };
				if (grab && grab.status !== 'no_match' && grab.task_id) {
					startPolling(grab.task_id);
					return;
				}
				toastStore.show({ message: `No source found for "${trackLabel}"`, type: 'error' });
				reset();
				return;
			}

			toastStore.show({ message: `Requested "${trackLabel}"`, type: 'success' });
			startPolling(json.task_id);
		} catch {
			toastStore.show({ message: "Couldn't request that track", type: 'error' });
			reset();
		}
	}

	// Conic-gradient progress ring, masked to a thin annulus.
	const ringStyle = $derived(
		'display:inline-block;width:18px;height:18px;border-radius:50%;' +
			`background:conic-gradient(var(--color-primary) ${progress}%, oklch(from var(--color-base-content) l c h / 0.18) 0);` +
			'-webkit-mask:radial-gradient(farthest-side,transparent 58%,#000 60%);' +
			'mask:radial-gradient(farthest-side,transparent 58%,#000 60%);'
	);
</script>

{#if phase !== 'gone'}
	<button
		type="button"
		class="btn btn-ghost btn-xs btn-circle shrink-0"
		aria-label="Request this track"
		{title}
		disabled={phase !== 'idle'}
		onclick={(e) => {
			e.stopPropagation();
			void request();
		}}
	>
		{#if phase === 'idle'}
			<Download class="h-3.5 w-3.5" />
		{:else if phase === 'loading'}
			<span class="loading loading-spinner loading-xs"></span>
		{:else if phase === 'polling'}
			<span style={ringStyle}></span>
		{:else if phase === 'done'}
			<Check class="h-3.5 w-3.5 text-success" />
		{/if}
	</button>
{/if}
