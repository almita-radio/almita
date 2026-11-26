// quick smoke: the pages load from the demo container and get recorded
export default async function (a) {
  await a.goto("status", "/status.html"); await a.sleep(8000); await a.shot("status", "status.png");
  await a.goto("monitor", "/index.html"); await a.sleep(8000); await a.shot("monitor", "monitor.png");
  await a.goto("observe", "/observe.html"); await a.sleep(5000); await a.shot("observe", "observe.png");
}
