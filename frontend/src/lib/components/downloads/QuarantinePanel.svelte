<script lang="ts">
	import { AlertTriangle, ShieldCheck, Trash2 } from 'lucide-svelte';

	import EmptyState from '$lib/components/EmptyState.svelte';
	import { getApiUrl } from '$lib/api/api-utils';
	import {
		deleteQuarantineEntry,
		getQuarantineQuery
	} from '$lib/queries/downloads/QuarantineQueries.svelte';

	const query = getQuarantineQuery();
	const del = deleteQuarantineEntry();

	// Release a quarantined slskd grab into the library. Text cycles
	// Accept -> Accepting... -> Accepted ✓ / No file found.
	type AcceptState = 'idle' | 'accepting' | 'accepted' | 'notfound';
	let acceptState = $state<Record<number, AcceptState>>({});

	function acceptLabel(id: number): string {
		switch (acceptState[id]) {
			case 'accepting':
				return 'Accepting...';
			case 'accepted':
				return 'Accepted ✓';
			case 'notfound':
				return 'No file found';
			default:
				return 'Accept';
		}
	}

	async function acceptEntry(id: number) {
		const current = acceptState[id];
		if (current === 'accepting' || current === 'accepted') return;
		acceptState = { ...acceptState, [id]: 'accepting' };
		try {
			const res = await fetch(getApiUrl(`/api/v1/me/spotify/accept-quarantine/${id}`), {
				method: 'POST',
				credentials: 'include'
			});
			if (!res.ok) throw new Error();
			const json = (await res.json()) as { status?: string };
			acceptState = {
				...acceptState,
				[id]: json && json.status === 'accepted' ? 'accepted' : 'notfound'
			};
		} catch {
			acceptState = { ...acceptState, [id]: 'idle' };
		}
	}

	// two-step inline confirm; UX-5/§8.9 require confirmation for this permanent deletion
	let confirmId = $state<number | null>(null);

	function requestDelete(id: number) {
		if (confirmId === id) {
			del.mutate(id);
			confirmId = null;
		} else {
			confirmId = id;
			setTimeout(() => {
				if (confirmId === id) confirmId = null;
			}, 3000);
		}
	}

	function fmtDate(ts: number): string {
		return new Date(ts * 1000).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		});
	}

	function basename(path: string): string {
		const parts = path.split(/[\\/]/);
		return parts[parts.length - 1] || path;
	}
</script>

{#if query.isLoading}
	<div class="space-y-2">
		<div class="skeleton h-14 w-full rounded-xl"></div>
		<div class="skeleton h-14 w-full rounded-xl"></div>
	</div>
{:else if query.isError}
	<div class="alert alert-error">Failed to load quarantine entries.</div>
{:else if (query.data?.items.length ?? 0) === 0}
	<EmptyState
		icon={ShieldCheck}
		title="Nothing quarantined"
		description="Files that fail verification are held here. There's nothing to review."
	/>
{:else}
	<div class="space-y-2">
		{#each query.data?.items ?? [] as entry (entry.id)}
			<div
				class="flex items-center gap-3 rounded-xl border border-base-content/5 bg-base-200/50 p-3 backdrop-blur-sm"
			>
				<AlertTriangle class="h-5 w-5 shrink-0 text-warning" aria-hidden="true" />
				<div class="min-w-0 flex-1">
					<p class="truncate text-sm font-medium" title={entry.filename}>
						{basename(entry.filename)}
					</p>
					<p class="truncate text-xs text-base-content/55">
						{entry.username}
						<span class="text-base-content/30"> · </span>{entry.reason}
						<span class="text-base-content/30"> · </span>{fmtDate(entry.quarantined_at)}
					</p>
				</div>
				<button
					class="btn btn-xs gap-1 btn-success"
					onclick={() => void acceptEntry(entry.id)}
					disabled={acceptState[entry.id] === 'accepting' || acceptState[entry.id] === 'accepted'}
				>
					{acceptLabel(entry.id)}
				</button>
				<button
					class="btn btn-xs gap-1 {confirmId === entry.id ? 'btn-error' : 'btn-ghost'}"
					onclick={() => requestDelete(entry.id)}
					disabled={del.isPending}
				>
					<Trash2 class="h-3.5 w-3.5" />
					{confirmId === entry.id ? 'Confirm - delete & allow retry' : 'Delete & allow retry'}
				</button>
			</div>
		{/each}
	</div>
{/if}
