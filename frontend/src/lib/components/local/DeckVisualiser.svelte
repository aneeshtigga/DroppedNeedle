<script lang="ts">
	// Audio-reactive spectrum visualiser intentionally disabled.
	//
	// The previous implementation ran a requestAnimationFrame loop that read the FFT and
	// repainted a full-width canvas every frame whenever audio was playing - a continuous
	// per-frame GPU/CPU cost that melted the Deck view. There is now no RAF loop and no
	// canvas 2D context, so the component adds zero per-frame work.
	interface Props {
		reducedMotion?: boolean;
	}

	// Kept for call-site compatibility; no longer used now that the loop is gone.
	let { reducedMotion: _reducedMotion = false }: Props = $props();
</script>
