const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage();
  
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));
  page.on('pageerror', err => console.log('PAGE ERROR:', err.message));
  
  await page.goto('http://127.0.0.1:8000/static/dashboard.html');
  await page.waitForTimeout(2000);
  
  const connectBtn = await page.$('#btn-connect-ros');
  if (connectBtn) {
    console.log("Found connect button, clicking...");
    await connectBtn.click();
    await page.waitForTimeout(2000);
  } else {
    console.log("Connect button not found!");
  }
  
  await browser.close();
})();
