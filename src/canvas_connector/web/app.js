'use strict';
const clients = {
  chatgpt: {name:'ChatGPT', description:'Use your hosted connector address in ChatGPT’s custom MCP setup. Availability depends on your plan and workspace settings.', url:'https://developers.openai.com/api/docs/guides/custom-mcp-server', steps:['Open ChatGPT’s custom MCP server setup.','Paste the connector address and select OAuth.','Choose your school and sign in through Canvas.'], local:''},
  claude: {name:'Claude', description:'Connect through Claude’s connector settings. Remote connections also work in supported Claude Desktop and Cowork experiences.', url:'https://claude.com/docs/connectors/building/authentication', steps:['Open Claude Settings, then Connectors, and add a custom connector.','Enter the connector address and start authorization.','Choose your school, select access, and sign in through Canvas.'], local:'Claude’s remote connectors need a reachable HTTPS endpoint, including when used from the desktop app.'},
  codex: {name:'Codex', description:'Use a hosted connection with OAuth, or launch the connector locally for your personal Canvas account.', url:'https://developers.openai.com/codex/mcp', steps:['Add your hosted endpoint: codex mcp add canvas --url <connector-address>','Sign in: codex mcp login canvas','Open a chat and ask for your Canvas dashboard.'], local:'For personal local use, register the full path to canvas-connector with the stdio argument and CANVAS_ENV_PATH pointing to your private credentials file.'},
  code: {name:'Claude Code', description:'Add the remote connector over HTTP, or use a personal local stdio server.', url:'https://code.claude.com/docs/en/mcp', steps:['Add your hosted endpoint: claude mcp add --transport http canvas <connector-address>','Run /mcp in Claude Code and authenticate Canvas.','Ask for your Canvas dashboard to check the connection.'], local:'Local alternative: claude mcp add --transport stdio canvas -- /absolute/path/canvas-connector stdio. Configure CANVAS_ENV_PATH privately before launch.'}
};
const tabs = [...document.querySelectorAll('[role=tab]')];
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
document.querySelector('#copy').addEventListener('click', async () => {
  const status = document.querySelector('#copy-status');
  const value = document.querySelector('#endpoint').value.trim();
  try { const url = new URL(value); if(url.protocol!=='https:' || url.username || url.password || url.search || url.hash || !url.pathname.endsWith('/mcp')) throw new Error(); }
  catch {status.textContent='Enter an HTTPS connector address ending in /mcp.';document.querySelector('#endpoint').focus();return;}
  try {await navigator.clipboard.writeText(value);status.textContent='Connector address copied.';}
  catch {document.querySelector('#endpoint').select();status.textContent='Select the address and copy it with your keyboard.';}
});
fetch('./connection.json', {credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(config=>{
  if(config?.source_url){const link=document.querySelector('#download-source');link.href=config.source_url;link.hidden=false;}
  if(config?.hosted && config.endpoint){document.querySelector('#endpoint').value=config.endpoint;document.querySelector('#endpoint-note').textContent='This server’s connector address. Continue in your assistant to choose a school and sign in.';}
}).catch(()=>{});
