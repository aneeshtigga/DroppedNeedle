<script lang="ts">
	import { goto } from '$app/navigation';
	import { toastStore } from '$lib/stores/toast';
	import { importSpotifyCsv, type SpotifyCsvTrack } from '$lib/api/playlists';
	import { importingPlaylists } from '$lib/stores/importingPlaylists.svelte';
	import { authStore } from '$lib/stores/authStore.svelte';
	import { invalidateQueriesWithPersister } from '$lib/queries/QueryClient';
	import { PlaylistQueryKeyFactory } from '$lib/queries/playlists/PlaylistQueryKeyFactory';

	interface Props {
		/** Called after at least one CSV imported successfully (e.g. to refetch the list). */
		onimported?: () => void;
	}

	let { onimported }: Props = $props();

	let fileInput = $state<HTMLInputElement | null>(null);
	let importing = $state(false);

	function openPicker() {
		if (importing) return;
		fileInput?.click();
	}

	// Tolerant CSV parser that handles quoted fields, escaped quotes and CRLF. Kept
	// dependency-free deliberately (mirrors the Exportify export shape).
	function parseCsv(text: string): string[][] {
		text = text.replace(/^﻿/, '');
		const rows: string[][] = [];
		let row: string[] = [];
		let field = '';
		let inQuotes = false;
		for (let i = 0; i < text.length; i++) {
			const c = text[i];
			if (inQuotes) {
				if (c === '"') {
					if (text[i + 1] === '"') {
						field += '"';
						i++;
					} else {
						inQuotes = false;
					}
				} else {
					field += c;
				}
			} else if (c === '"') {
				inQuotes = true;
			} else if (c === ',') {
				row.push(field);
				field = '';
			} else if (c === '\n' || c === '\r') {
				if (c === '\r' && text[i + 1] === '\n') i++;
				row.push(field);
				field = '';
				if (row.length > 1 || row[0] !== '') rows.push(row);
				row = [];
			} else {
				field += c;
			}
		}
		if (field !== '' || row.length) {
			row.push(field);
			rows.push(row);
		}
		return rows;
	}

	// Exact header match first, then substring fallback (Exportify uses e.g.
	// "Artist Name(s)", "Track Duration (ms)").
	function headerIndex(headers: string[], names: string[]): number {
		for (const n of names) {
			const i = headers.indexOf(n);
			if (i !== -1) return i;
		}
		for (const n of names) {
			const i = headers.findIndex((h) => h.includes(n));
			if (i !== -1) return i;
		}
		return -1;
	}

	function intOrNull(v: string | undefined): number | null {
		const n = parseInt((v ?? '').trim(), 10);
		return Number.isFinite(n) ? n : null;
	}

	function rowsToTracks(rows: string[][]): SpotifyCsvTrack[] {
		if (!rows.length) return [];
		const headers = rows[0].map((h) => h.trim().toLowerCase());
		const cols = {
			track: headerIndex(headers, ['track name', 'name']),
			artist: headerIndex(headers, ['artist name(s)', 'artist name', 'artist']),
			album: headerIndex(headers, ['album name', 'album']),
			isrc: headerIndex(headers, ['isrc']),
			duration: headerIndex(headers, ['track duration (ms)', 'duration (ms)', 'duration']),
			trackNo: headerIndex(headers, ['track number', 'track#', 'track #']),
			discNo: headerIndex(headers, ['disc number', 'disc#', 'disc #'])
		};
		if (cols.track === -1 || cols.artist === -1) {
			throw new Error('missing Track Name / Artist columns');
		}
		const tracks: SpotifyCsvTrack[] = [];
		for (let r = 1; r < rows.length; r++) {
			const row = rows[r];
			const name = (row[cols.track] || '').trim();
			if (!name) continue;
			let duration: number | null = null;
			if (cols.duration !== -1) {
				const value = parseInt((row[cols.duration] || '').trim(), 10);
				// Exportify emits milliseconds; small values are already seconds.
				if (Number.isFinite(value)) duration = value >= 1000 ? Math.round(value / 1000) : value;
			}
			tracks.push({
				track_name: name,
				artist_name: (cols.artist !== -1 ? row[cols.artist] || '' : '').trim(),
				album_name: (cols.album !== -1 ? row[cols.album] || '' : '').trim(),
				// ISRC MUST be sent when present - it drives server-side ISRC-exact matching.
				isrc: (cols.isrc !== -1 ? row[cols.isrc] || '' : '').trim(),
				track_number: cols.trackNo !== -1 ? intOrNull(row[cols.trackNo]) : null,
				disc_number: cols.discNo !== -1 ? intOrNull(row[cols.discNo]) : null,
				duration
			});
		}
		return tracks;
	}

	async function importFile(file: File): Promise<string> {
		const name = file.name.replace(/\.csv$/i, '').trim() || 'Imported Playlist';
		const tracks = rowsToTracks(parseCsv(await file.text()));
		if (!tracks.length) throw new Error('no tracks parsed');
		const withIsrc = tracks.filter((t) => t.isrc).length;
		toastStore.show({
			message: `"${name}": importing ${tracks.length} tracks (${withIsrc} with ISRC)…`,
			type: 'info'
		});
		// Post the full list to the Spotify import-csv endpoint: the server creates the
		// playlist and runs ISRC-exact + name matching / cover resolution in the background.
		const { playlist_id } = await importSpotifyCsv(name, tracks);
		// Show the playlist in the list right away (with its cover loader) before the first
		// SSE arrives; the loader clears on the terminal `playlist_imported` (done) event.
		importingPlaylists.add(playlist_id);
		void invalidateQueriesWithPersister({
			queryKey: PlaylistQueryKeyFactory.list(authStore.user?.id)
		});
		return playlist_id;
	}

	async function onFilesSelected(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const files = Array.from(input.files ?? []);
		input.value = '';
		if (!files.length) return;

		importing = true;
		let lastId: string | null = null;
		let done = 0;
		for (const file of files) {
			try {
				lastId = await importFile(file);
				done++;
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'import failed';
				toastStore.show({ message: `"${file.name}": ${msg}`, type: 'error' });
			}
		}
		importing = false;

		if (done > 0) {
			toastStore.show({
				message: `Imported ${done} playlist${done === 1 ? '' : 's'}`,
				type: 'success'
			});
			onimported?.();
			// A single import jumps straight to the new playlist; a batch stays on the list.
			if (files.length === 1 && lastId) await goto(`/playlists/${lastId}`);
		}
	}
</script>

<button
	type="button"
	class="btn btn-sm gap-1.5 border-0 bg-[#8b5cf6] text-white hover:bg-[#7c3aed]"
	onclick={openPicker}
	disabled={importing}
>
	{#if importing}
		<span class="loading loading-spinner loading-xs"></span>
	{:else}
		<span aria-hidden="true">⬇</span>
	{/if}
	Import Spotify CSV
</button>
<input
	bind:this={fileInput}
	type="file"
	accept=".csv,text/csv"
	multiple
	class="hidden"
	onchange={onFilesSelected}
/>
