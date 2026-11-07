const url = process.env.HUB_URL;
if (!url || !/^https:\/\//.test(url)) {
  throw new Error("Set HUB_URL to your hub's https address, e.g. https://yourlaptop.tail1234.ts.net");
}
module.exports = {
  appId: "dev.shikhar.laptophub", appName: "REDLINE", webDir: "www",
  server: { url, cleartext: false, allowNavigation: [new URL(url).host] },
};
