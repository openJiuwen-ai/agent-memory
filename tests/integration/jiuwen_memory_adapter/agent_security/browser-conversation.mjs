/** Isolated headless browser; never reads the user's browser profile. */
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.MINDVAULT_PLAYWRIGHT));
const [url, choice] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: process.env.MINDVAULT_BROWSER, headless: true });
try {
  const context = await browser.newContext();
  await context.addCookies([{ name: "owner", value: "valid", url, httpOnly: true, sameSite: "Strict" }]);
  const page = await context.newPage();
  await page.goto(url);
  await page.locator("#message").fill("pipeline-job");
  await page.locator("#send").click();
  let approvals = 0;
  const labels = { CONTINUE_ORIGINAL: "1. 继续外发（仅本次）", ABORT: "2. 中止外发", SEND_REDACTED: "3. 脱敏外发" };
  for (let i = 0; i < 600; i++) {
    const card = page.locator("jiuwen-security-approvals article").first();
    if (await card.count() && await card.getByRole("button").first().isEnabled()) {
      const text = await card.innerText();
      assert.ok(!text.includes("11010519491231002X"));
      assert.ok(!text.includes("13800138000"));
      const selected = /send_report|agent\.response/u.test(text) ? choice : "SEND_REDACTED";
      await card.getByRole("button", { name: labels[selected], exact: true }).click();
      approvals++;
    }
    if (await page.locator("#status").innerText() === "本次运行结束") break;
    await page.waitForTimeout(100);
  }
  assert.equal(await page.locator("#status").innerText(), "本次运行结束");
  assert.ok(approvals >= (choice === "ABORT" ? 1 : 2));
  const text = await page.locator("#transcript").innerText();
  assert.ok(text.includes(choice === "ABORT" ? "操作已中止" : "final [PRIVATE]"), text);
  assert.ok(!text.includes("11010519491231002X"));
  console.log(JSON.stringify({ passed: true, approvals, choice }));
} finally { await browser.close(); }
