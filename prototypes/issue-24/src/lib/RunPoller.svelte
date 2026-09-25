<script>
  import {onMount} from 'svelte';
  import {startFixturePolling} from './fixturePolling.js';
  export let scenario;
  export let onSnapshot;
  onMount(() => startFixturePolling({getScenario: () => scenario, onSnapshot}));
</script>
<span data-run-poller hidden></span>
