// A fixture adapter with the same start/stop ownership a read-only Run API poller needs.
// Its caller owns the AbortSignal and discards stale responses on unmount.
export async function readFixtureRun(scenario, sequence, signal) {
  if (signal.aborted) throw new DOMException('Polling stopped', 'AbortError');
  return {scenario, sequence: sequence + 1};
}

export function startFixturePolling({getScenario, onSnapshot, intervalMs = 1200}) {
  let sequence = 0;
  let active = true;
  let inFlight = false;
  const controller = new AbortController();
  async function poll() {
    if (!active || inFlight || getScenario() !== 'running') return;
    inFlight = true;
    try {
      const result = await readFixtureRun(getScenario(), sequence, controller.signal);
      if (active && result.scenario === getScenario()) {
        sequence = result.sequence;
        onSnapshot(result);
      }
    } catch (error) {
      if (error?.name !== 'AbortError') throw error;
    } finally {
      inFlight = false;
    }
  }
  const timer = setInterval(poll, intervalMs);
  return () => {active = false; clearInterval(timer); controller.abort();};
}
