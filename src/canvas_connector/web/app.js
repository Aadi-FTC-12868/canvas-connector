'use strict';
const clients = {
  chatgpt: {name:'ChatGPT', description:'Choose a personal connection from your computer or a hosted connector. Availability depends on your OpenAI account and workspace permissions.', url:'https://developers.openai.com/api/docs/guides/custom-mcp-server', steps:['Open ChatGPT’s custom MCP server setup.','Paste the connector address and select OAuth.','Choose your school and sign in through Canvas.'], local:''},
  claude: {name:'Claude', description:'Connect through Claude’s connector settings. Remote connections also work in supported Claude Desktop and Cowork experiences.', url:'https://claude.com/docs/connectors/building/authentication', steps:['Open Claude Settings, then Connectors, and add a custom connector.','Enter the connector address and start authorization.','Choose your school, select access, and sign in through Canvas.'], local:'Claude’s remote connectors need a reachable HTTPS endpoint, including when used from the desktop app.'},
  codex: {name:'Codex', description:'Use a hosted connection with OAuth, or launch the connector locally for your personal Canvas account.', url:'https://developers.openai.com/codex/mcp', steps:['Add your hosted endpoint: codex mcp add canvas --url <connector-address>','Sign in: codex mcp login canvas','Open a chat and ask for your Canvas dashboard.'], local:'For personal local use, register the full path to canvas-connector with the stdio argument and CANVAS_ENV_PATH pointing to your private credentials file.'},
  code: {name:'Claude Code', description:'Add the remote connector over HTTP, or use a personal local stdio server.', url:'https://code.claude.com/docs/en/mcp', steps:['Add your hosted endpoint: claude mcp add --transport http canvas <connector-address>','Run /mcp in Claude Code and authenticate Canvas.','Ask for your Canvas dashboard to check the connection.'], local:'Local alternative: claude mcp add --transport stdio canvas -- /absolute/path/canvas-connector stdio. Configure CANVAS_ENV_PATH privately before launch.'}
};
const tabs = [...document.querySelectorAll('[role=tab]')];
const repository = 'https://github.com/Aadi-FTC-12868/canvas-connector';
const pluginCommands = {
  codex: [`codex plugin marketplace add ${repository}.git`, 'codex plugin add canvas-connector-personal@built-by-aadi'],
  code: [`claude plugin marketplace add ${repository}.git`, 'claude plugin install canvas-connector-personal@built-by-aadi']
};
let downloads = {desktop:null, plugin:null, source:null};
function showDownloads(client) {
  document.querySelector('#tunnel-install').hidden = client !== 'chatgpt';
  document.querySelector('#download-tunnel-source').hidden = !downloads.source;
  document.querySelector('#tunnel-copy-status').textContent = '';
  document.querySelector('#desktop-install').hidden = !(downloads.desktop && client === 'claude');
  document.querySelector('#plugin-install').hidden = !['codex','code'].includes(client);
  document.querySelector('#download-plugin').hidden = !downloads.plugin;
  document.querySelector('#plugin-title').textContent = `Personal local plugin for ${clients[client].name}`;
  const commands = pluginCommands[client] || [];
  document.querySelector('#plugin-register').value = commands[0] || '';
  document.querySelector('#plugin-add').value = commands[1] || '';
  document.querySelector('#plugin-account').textContent = client === 'code'
    ? 'Enter your Canvas address and personal token in Claude Code’s plugin configuration fields. Keep writes off unless you need them, then reload the plugin and check the connected account.'
    : 'After installation, restart your session and run the plugin’s setup skill. It guides you through a private local credentials file. Never paste a token into chat.';
  document.querySelector('#codex-plugin-link').hidden = client !== 'codex';
  document.querySelector('#codex-link-note').hidden = client !== 'codex';
  document.querySelector('#plugin-copy-status').textContent = '';
}
function downloadTarget(value, extension) {
  if(typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value, document.baseURI);
    if(url.username || url.password || url.hash || !url.pathname.endsWith(extension)) return null;
    if(url.protocol !== 'https:' && !(url.protocol === 'http:' && url.origin === location.origin)) return null;
    return url.href;
  } catch {return null;}
}
function choose(button) {
  const entry = clients[button.dataset.client];
  tabs.forEach(tab => { const selected = tab === button; tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1; });
  document.querySelector('#guide').setAttribute('aria-labelledby', button.id);
  document.querySelector('#guide-title').textContent = `Connect in ${entry.name}`;
  document.querySelector('#guide-description').textContent = entry.description;
  const list = document.querySelector('#steps'); list.replaceChildren();
  entry.steps.forEach(text => { const li = document.createElement('li'); li.textContent = text; list.append(li); });
  const link = document.querySelector('#client-link'); link.href = entry.url; link.textContent = `Open ${entry.name} setup guide`;
  document.querySelector('#local-note').textContent = entry.local;
  showDownloads(button.dataset.client);
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => choose(tab));
  tab.addEventListener('keydown', event => {
    let target;
    if(event.key==='ArrowRight') target=tabs[(index+1)%tabs.length];
    if(event.key==='ArrowLeft') target=tabs[(index+tabs.length-1)%tabs.length];
    if(event.key==='Home') target=tabs[0]; if(event.key==='End') target=tabs.at(-1);
    if(target){event.preventDefault();choose(target);target.focus();}
  });
});
document.querySelector('#copy-tunnel').addEventListener('click', async () => {
  const input = document.querySelector('#tunnel-command');
  const status = document.querySelector('#tunnel-copy-status');
  try {await navigator.clipboard.writeText(input.value);status.textContent='Command copied. Run it from the extracted folder containing scripts/.';}
  catch {input.select();status.textContent='Copy the selected command with your keyboard.';}
});
document.querySelector('#copy-plugin').addEventListener('click', async () => {
  const status = document.querySelector('#plugin-copy-status');
  const value = [document.querySelector('#plugin-register').value, document.querySelector('#plugin-add').value].join('\n');
  try {await navigator.clipboard.writeText(value);status.textContent='Install commands copied. Run them in your terminal, then finish account setup.';}
  catch {document.querySelector('#plugin-register').select();status.textContent='Copy each command with your keyboard and run them in order.';}
});
document.querySelector('#copy').addEventListener('click', async () => {
  const status = document.querySelector('#copy-status');
  const value = document.querySelector('#endpoint').value.trim();
  try { const url = new URL(value); if(url.protocol!=='https:' || url.username || url.password || url.search || url.hash || !url.pathname.endsWith('/mcp')) throw new Error(); }
  catch {status.textContent='Enter an HTTPS connector address ending in /mcp.';document.querySelector('#endpoint').focus();return;}
  try {await navigator.clipboard.writeText(value);status.textContent='Connector address copied.';}
  catch {document.querySelector('#endpoint').select();status.textContent='Select the address and copy it with your keyboard.';}
});
choose(tabs.find(tab=>tab.getAttribute('aria-selected')==='true'));
fetch('./connection.json', {credentials:'same-origin',cache:'no-store'}).then(r=>r.ok?r.json():null).then(config=>{
  const source = downloadTarget(config?.source_url, '.zip');
  downloads.source = source;
  if(source){const link=document.querySelector('#download-source');link.href=source;link.hidden=false;document.querySelector('#download-tunnel-source').href=source;}
  downloads.desktop = downloadTarget(config?.desktop_bundle_url, '.mcpb');
  downloads.plugin = downloadTarget(config?.plugin_bundle_url, '.zip');
  if(downloads.desktop) document.querySelector('#download-desktop').href = downloads.desktop;
  if(downloads.plugin) document.querySelector('#download-plugin').href = downloads.plugin;
  showDownloads(tabs.find(tab=>tab.getAttribute('aria-selected')==='true').dataset.client);
  if(config?.hosted && config.endpoint){document.querySelector('#endpoint').value=config.endpoint;document.querySelector('#endpoint-note').textContent='This server’s connector address. Continue in your assistant to choose a school and sign in.';}
}).catch(()=>{});
