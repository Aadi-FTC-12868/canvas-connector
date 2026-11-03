'use strict';
const promptInput = document.querySelector('#setup-prompt');
const promptStatus = document.querySelector('#prompt-copy-status');
document.querySelector('#copy-setup-prompt').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(promptInput.value);
    promptStatus.textContent = 'Prompt copied. Paste it into your agent without adding your Canvas token.';
  } catch {
    promptInput.focus();
    promptInput.select();
    promptStatus.textContent = 'Copy the selected prompt with your keyboard. Keep your Canvas token separate.';
  }
});
fetch('./connection.json', {credentials:'same-origin',cache:'no-store'})
  .then(response => response.ok ? response.json() : null)
  .then(config => {
    if(typeof config?.source_url !== 'string' || !config.source_url.trim()) return;
    const url = new URL(config.source_url, document.baseURI);
    if(url.username || url.password || url.hash || !url.pathname.endsWith('.zip')) return;
    if(url.protocol !== 'https:' && !(url.protocol === 'http:' && url.origin === location.origin)) return;
    const link = document.querySelector('#local-source-download');
    link.href = url.href;
    link.hidden = false;
  }).catch(() => {});
