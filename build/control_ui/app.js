const token = location.hash.slice(1);
const $ = id => document.getElementById(id);
const joints = [['shoulder_pan','Base'],['shoulder_lift','Shoulder'],['elbow_flex','Elbow'],['wrist_flex','Wrist flex'],['wrist_roll','Wrist roll'],['gripper','Gripper']];
let selected = 'shoulder_pan', last = 0, busy = false, recording = false;
for (const [name,label] of joints) {
  const button = document.createElement('button');
  button.className = 'joint'; button.dataset.joint = name;
  const title = document.createElement('span'); title.textContent = label;
  const angle = document.createElement('span'); angle.className = 'angle'; angle.textContent = '—'; angle.id = `angle-${name}`;
  button.append(title,angle); button.onclick = () => { selected=name; selectJoint(); };
  $('joints').append(button);
}
function selectJoint(){ document.querySelectorAll('.joint').forEach(b=>{ const active=b.dataset.joint===selected;b.classList.toggle('selected',active);b.setAttribute('aria-pressed',active); });document.querySelectorAll('[data-taught]').forEach(b=>b.disabled=busy||selected!=='shoulder_pan'); }
selectJoint();
async function api(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Control-Token':token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const data=await response.json(); if(!response.ok) throw Error(data.error||'Local server error.');return data;
}
async function action(path,body={}){ $('error').textContent='';try{await api(path,body);await poll();}catch(e){$('error').textContent=e.message;} }
function send(text,preview=$('preview').checked){return action('/api/command',{text,preview});}
$('command-form').onsubmit=async e=>{e.preventDefault();const text=$('command').value.trim();if(!text)return;await send(text);if(!$('error').textContent)$('command').value='';};
document.querySelectorAll('[data-command]').forEach(b=>b.onclick=()=>send(b.dataset.command,false));
document.querySelectorAll('[data-taught]').forEach(b=>b.onclick=()=>send(b.dataset.taught));
$('stop').onclick=()=>action('/api/stop');
for(const [id,sign] of [['minus',-1],['plus',1]]) $(id).onclick=()=>{const amount=Number($('degrees').value);if(!Number.isFinite(amount)||amount<=0){$('error').textContent='Enter a positive degree amount.';return;}send(`jog ${selected} ${amount*sign}`);};
$('record').onclick=()=>action(recording?'/api/finish':'/api/record',{preview:$('preview').checked});
document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.code==='Space'){e.preventDefault();if(!$('record').disabled)$('record').click();}});
function controls(){
  selectJoint();
  document.querySelectorAll('[data-command],#send,#minus,#plus,#preview').forEach(b=>b.disabled=busy);
  $('record').disabled=busy&&!recording;$('record').textContent=recording?'Finish recording':'Record voice';$('record').classList.toggle('recording',recording);
  $('voice-state').textContent=recording?'Recording · click to finish':busy?'Command in progress…':'Microphone off · Ctrl+Space to record';
}
async function poll(){
  try{
    const data=await api('/api/state');busy=data.busy;recording=data.recording;
    $('stop').disabled=false;document.querySelectorAll('.joint').forEach(b=>b.disabled=false);
    $('connection').textContent=data.demo?'Demo · no hardware access':busy?'Local interface · command active':'Local interface · idle';controls();
    const log=$('log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<60;
    const fresh=data.events.filter(e=>e.id>last);
    for(const event of fresh){const row=document.createElement('div');row.className=`entry ${event.kind}`;row.textContent=event.text;log.append(row);last=event.id;}
    while(log.children.length>500)log.firstChild.remove();
    if(atBottom)log.scrollTop=log.scrollHeight;
    // CLI JSON can span multiple output lines. Read angles from the latest complete read result.
    const output=data.events.filter(e=>e.kind==='result').map(e=>e.text).join('\n');
    const matches=[...output.matchAll(/"degrees"\s*:\s*(\{[^}]+\})/g)];
    if(matches.length){try{const values=JSON.parse(matches.at(-1)[1]);for(const [name]of joints)if(Number.isFinite(values[name]))$(`angle-${name}`).textContent=`${values[name].toFixed(1)}°`;}catch{}}
  }catch(e){$('connection').textContent='Local interface disconnected';$('error').textContent=e.message;document.querySelectorAll('button').forEach(b=>b.disabled=true);}
}
async function loop(){await poll();setTimeout(loop,500);}loop();
