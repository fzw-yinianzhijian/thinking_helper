// Run with Playwright CLI run-code. Reads two existing topics; no model calls.
async (page) => {
  const topics = await page.locator('[data-topic]').evaluateAll(nodes => nodes.slice(0, 2).map(node => ({
    id: node.dataset.topic, title: node.childNodes[0].textContent.trim(),
  })));
  if (topics.length < 2) return {skipped: true, reason: 'Two existing workspaces are required.'};
  const original = await page.evaluate(() => localStorage.getItem('thinking-topic'));
  const [oldTopic, newTopic] = topics;
  const pattern = '**/api/topics/' + oldTopic.id;
  await page.route(pattern, async route => {
    await new Promise(resolve => setTimeout(resolve, 1200));
    await route.continue();
  });
  try {
    const started = page.waitForRequest(request => request.url().endsWith('/api/topics/' + oldTopic.id));
    await page.locator('[data-topic="' + oldTopic.id + '"]').click();
    await started;
    await page.locator('[data-topic="' + newTopic.id + '"]').click();
    await page.waitForFunction(title => document.querySelector('#topicTitle').textContent === title, newTopic.title);
    await page.waitForTimeout(1600);
    const view = await page.evaluate(() => ({selected: localStorage.getItem('thinking-topic'), title: document.querySelector('#topicTitle').textContent}));
    if (view.selected !== newTopic.id || view.title !== newTopic.title) throw new Error('Late topic response overwrote the current workspace.');
    return {passed: true, scenario: 'late_topic_response_isolation', delayed_ms: 1200};
  } finally {
    await page.unroute(pattern);
    if (original && topics.some(topic => topic.id === original)) await page.locator('[data-topic="' + original + '"]').click();
  }
}
