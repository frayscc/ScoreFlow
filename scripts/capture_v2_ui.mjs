import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const [baseUrl, token, projectId, periodId, outputDir] = process.argv.slice(2);
if (![baseUrl, token, projectId, periodId, outputDir].every(Boolean)) {
  throw new Error("用法：node scripts/capture_v2_ui.mjs <baseUrl> <token> <projectId> <periodId> <outputDir>");
}

const browser = await chromium.launch({ headless:true });
try {
  const page = await browser.newPage({ viewport:{ width:1440, height:1000 }, deviceScaleFactor:1 });
  const url = `${baseUrl}/session/bootstrap?token=${encodeURIComponent(token)}&project=${encodeURIComponent(projectId)}&period=${encodeURIComponent(periodId)}`;
  await page.goto(url, { waitUntil:"networkidle" });
  const roster = Array.from({ length:49 }, (_, index) => `${index + 1} 演示学生${index + 1}`).join("\n");
  await page.locator(".roster-panel textarea").fill(roster);
  await page.getByText("已识别49人").waitFor();
  await page.locator(".roster-panel").screenshot({ path:`${outputDir}/v2-roster-paste.png` });
  await page.locator(".grouping-workbench").screenshot({ path:`${outputDir}/v2-grouping-workbench.png` });
} finally {
  await browser.close();
}
