export function createPoller(load, {interval = 1500, retry = 5000, onData = () => {}, onError = () => {}} = {}) {
  let active = false;
  let timer;
  let controller;
  let generation = 0;
  async function tick(token) {
    if (!active || token !== generation) return;
    const current = new AbortController();
    controller = current;
    let delay = interval;
    try {
      const value = await load(current.signal);
      if (active && token === generation) onData(value);
    } catch (error) {
      delay = retry;
      if (active && token === generation && error?.name !== 'AbortError') onError(error);
    } finally {
      if (controller === current) controller = undefined;
      if (active && token === generation) timer = setTimeout(() => tick(token), delay);
    }
  }
  return {
    start() {if (active) return; active = true; tick(++generation);},
    stop() {active = false; generation++; clearTimeout(timer); controller?.abort();},
    get active() {return active;},
  };
}
