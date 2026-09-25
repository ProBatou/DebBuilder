<script>
  import {onMount} from 'svelte';
  import {startFixturePolling} from './fixturePolling.js';
  export let scenario;
  export let onSnapshot = () => {};
  let pollCount = 0;
  onMount(() => startFixturePolling({getScenario: () => scenario, onSnapshot: (snapshot) => {pollCount += 1;onSnapshot(snapshot);}}));
</script>
<span data-run-poller data-poll-count={pollCount} hidden></span>
