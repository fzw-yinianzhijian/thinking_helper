import {VoiceIO} from './voice.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const state = {id:localStorage.getItem('thinking-topic'), data:null, controller:null, generation:0, speaking:Promise.resolve(), playbackWrites:new Set(),refreshSeq:0,selectionSeq:0, lastTranscript:'', lastTranscriptAt:0};
const labels = {hypothesis:'未定判断',supported:'有引用 · 待核查',contested:'存在分歧',queued:'等待运行',running:'正在推进',completed:'完成',failed:'失败',cancelled:'已取消',stale:'基于旧版本',pending:'待审阅',applied:'已整合',accepted:'已接受建议',noop:'无新变化',rejected:'未采纳',no_change:'没有实质变化',duplicate:'重复结果'};
const showError = error => { $('error').textContent=String(error?.message || error); $('error').hidden=false; };
function date(value) { return value ? new Date(typeof value==='number'?value*1000:value).toLocaleString('zh-CN',{hour12:false}) : '—'; }
function stableHTML(id,html) {const node=$(id);if(node._lastHTML!==html){node.innerHTML=html;node._lastHTML=html;}}
function safeLink(source) { try {const url=new URL(source.url); if(!['http:','https:'].includes(url.protocol))return ''; return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(source.title || source.id)} ↗</a>`;} catch {return '';} }
async function api(path, options={}) {
  const response = await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  const data=await response.json(); if(!response.ok)throw new Error(data.error || data.detail || `请求失败 ${response.status}`); return data;
}
const post = (path,body={}) => api(path,{method:'POST',body:JSON.stringify(body)});
function action(fn) { return async (...args) => {try{$('error').hidden=true;await fn(...args);}catch(error){showError(error);}}; }

const voice = new VoiceIO({silenceMs:1300,
  onPartial:text=>{$('chatText').value=text;},
  onFinal:text=>{
    if(!text?.trim())return;
    if(text===state.lastTranscript && Date.now()-state.lastTranscriptAt<2500)return;
    state.lastTranscript=text;state.lastTranscriptAt=Date.now();$('chatText').value='';const normalized=text.replace(/[，。！？\s]/g,'');sendChat(text,/^(请)?(向我|给我)?汇报(一下)?(整体|当前|整体与变化)?$/.test(normalized));
  },
  onSpeech:()=>{stopReply(false);},
  onBackchannel:text=>{if(state.id)post(`/api/topics/${state.id}/observation`,{kind:'backchannel',text}).catch(showError);},
  onError:showError,
  onState:value=>{
    $('mic').classList.toggle('active',voice.isListening);
    $('mic').textContent=voice.isListening?'● 正在听，点击结束':'🎙 开始语音';
    $('voiceState').textContent=value.speaking?'正在说，可直接开口打断':voice.isListening?'正在听，允许停顿思考':'平静、自然，可随时打断';
  },
  onMetrics:value=>{
    if(value.firstPCM!=null){
      if(!state.firstPlaybackRecorded)$('latency').textContent=`TTS首包 ${Math.round(value.firstPCM)}ms · 出声为排程估计`;
      if(!state.firstPlaybackRecorded && state.activeReportId && state.submitStartedAt){
        state.firstPlaybackRecorded=true;
        const total=Math.round((value.scheduledStartAt ?? performance.now()+25)-state.submitStartedAt);
        $('latency').textContent=`提交到排程出声 ${total}ms · TTS首包 ${Math.round(value.firstPCM)}ms`;
        post(`/api/topics/${state.id}/observation`,{kind:'latency',report_id:state.activeReportId,first_pcm_ms:value.firstPCM,submit_to_scheduled_output_ms:total}).catch(showError);
      }
    }
  }
});

function stopReply(stopAudio=true) {
  state.generation++;state.controller?.abort();state.controller=null;
  if(stopAudio)voice.interrupt();
  state.speaking=Promise.resolve();$('send').disabled=false;
}

function addMessage(role,text='') {
  const item=document.createElement('div');item.className=`message ${role}`;
  const who=document.createElement('span');who.className='who';who.textContent=role==='user'?'你':'Thinking Helper';
  const content=document.createElement('span');content.textContent=text;item.append(who,content);$('messages').append(item);
  $('messages').scrollTop=$('messages').scrollHeight;return {item,content};
}

function queueSpeech(text,generation,reportId,topicId) {
  if(!$('voiceEnabled').checked||!/[\p{L}\p{N}]/u.test(text))return;
  const segmentId=`${reportId}:${++state.speechSequence}`;
  if(generation!==state.generation)return;
  // VoiceIO runs the current sentence and prefetches at most one next sentence.
  // Do not serialize enqueue behind playback completion.
  const playback=voice.enqueue(text).then(async result=>{
    if(result && result.firstPCM!=null){
      const receipt=post(`/api/topics/${topicId}/playback`,{text,complete:!result.cancelled,report_id:reportId,segment_id:segmentId,played_ms:result.playedMs||0});
      state.playbackWrites.add(receipt);
      receipt.then(()=>state.playbackWrites.delete(receipt),()=>state.playbackWrites.delete(receipt));
      await receipt;
    }
  }).catch(showError);
  state.speaking=Promise.all([state.speaking,playback]).catch(showError);
}

async function sendChat(text,report=false) {
  if(!state.id)return;
  stopReply();const generation=state.generation,topicId=state.id;
  state.submitStartedAt=performance.now();state.firstPlaybackRecorded=false;state.speechSequence=0;
  if($('voiceEnabled').checked){try{await voice.ensureAudio();}catch(error){if(generation===state.generation)showError(error);return;}}
  // Interrupt resolves the active segment in a microtask. Flush only receipts,
  // never wait for queued/playing audio; a slow write has a bounded 1200ms wait.
  await Promise.resolve();
  if(state.playbackWrites.size)await Promise.race([Promise.allSettled([...state.playbackWrites]),new Promise(resolve=>setTimeout(resolve,1200))]);
  if(generation!==state.generation || topicId!==state.id)return;
  const controller=new AbortController();state.controller=controller;
  $('chatText').value='';$('error').hidden=true;$('send').disabled=true;
  addMessage('user',report?'请向我汇报整体与变化。':text);
  const message=addMessage('assistant');let sentence='',reportId='',buffer='',first=true;const started=performance.now();
  try {
    const response=await fetch(`/api/topics/${topicId}/chat`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,report,voice_mode:$('voiceEnabled').checked}),signal:controller.signal});
    if(!response.ok){const e=await response.json();throw new Error(e.error||e.detail||'对话连接失败。');}
    const reader=response.body.getReader(),decoder=new TextDecoder();
    while(true){
      const {value,done}=await reader.read();if(done)break;
      buffer+=decoder.decode(value,{stream:true});let boundary;
      while((boundary=buffer.indexOf('\n\n'))>=0){
        const chunk=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
        const line=chunk.split('\n').find(x=>x.startsWith('data:'));if(!line)continue;
        const event=JSON.parse(line.slice(5));
        if(event.type==='start'){reportId=event.report_id;state.activeReportId=reportId;}
        if(event.type==='error')throw new Error(event.error);
        if(event.type==='delta'){
          if(generation!==state.generation)return;
          if(first){first=false;$('latency').textContent=`文字首字 ${Math.round(performance.now()-started)}ms`;}
          message.content.textContent+=event.text;sentence+=event.text;
          // Feed complete, short clauses into TTS while the model continues.
          let end;
          while((end=sentence.search(/[。！？\n]/))>=0){const part=sentence.slice(0,end+1);sentence=sentence.slice(end+1);queueSpeech(part,generation,reportId,topicId);}
          if(sentence.length>110){queueSpeech(sentence.slice(0,100),generation,reportId,topicId);sentence=sentence.slice(100);}
          $('messages').scrollTop=$('messages').scrollHeight;
        }
      }
    }
    if(sentence.trim())queueSpeech(sentence,generation,reportId,topicId);
  } catch(error) {
    if(error.name==='AbortError')message.item.classList.add('interrupted');else showError(error);
  } finally {
    if(generation===state.generation){state.controller=null;$('send').disabled=false;}
  }
}

function changePreview(candidate,data) {
  const before=data.jobs.find(j=>j.id===candidate.job_id)?.topic_snapshot?.document;
  const after=candidate.document;
  if(!before||!after)return '';
  const changes=[];
  for(const [field,label] of [['claims','判断'],['questions','问题']]){
    const oldItems=new Map((before[field]||[]).filter(x=>typeof x==='object').map(x=>[x.id,x]));
    const newItems=new Map((after[field]||[]).filter(x=>typeof x==='object').map(x=>[x.id,x]));
    for(const [id,item] of newItems){
      const old=oldItems.get(id);
      if(!old)changes.push(`新增${label}：${item.text}`);
      else if(old.text!==item.text||old.status!==item.status||JSON.stringify(old.source_ids)!==JSON.stringify(item.source_ids))changes.push(`调整${label}：${item.text}`);
    }
    for(const [id,item] of oldItems)if(!newItems.has(id))changes.push(`将移除${label}：${item.text}`);
  }
  return changes.length?`<ul class="change-list">${changes.slice(0,6).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p class="muted">本轮未改变判断与问题分支。</p>';
}

function render(data) {
  state.data=data;const t=data.topic,doc=t.document;
  $('empty').hidden=true;$('workspace').hidden=false;$('exportTopic').disabled=false;
  $('topicTitle').textContent=t.title;$('topicGoal').textContent=t.goal;$('version').textContent=`版本 ${t.version}`;
  $('summary').textContent=doc.summary || '目前还没有全局判断。先自由表达，或让后台推进一个小问题。';
  const sources=new Map((t.sources||[]).map(x=>[x.id,x]));
  $('claimCount').textContent=`（${doc.claims?.length||0}条）`;
  stableHTML('claims',(doc.claims||[]).map(c=>`<article class="claim"><span class="tag">${esc(labels[c.status]||c.status)}</span><p>${esc(c.text)}</p>${(c.source_ids||[]).map(id=>sources.has(id)?safeLink(sources.get(id)):'').join(' ')}</article>`).join('')||'<p class="muted">尚未形成观点，允许想法暂时零散。</p>');
  stableHTML('questions',(doc.questions||[]).map(q=>`<div class="question ${q.status==='resolved'?'resolved':''}">${esc(typeof q==='string'?q:q.text)}</div>`).join('')||'<p class="muted">还没有明确的问题分支。</p>');
  stableHTML('nextSteps',(doc.next_steps||[]).map(x=>`<li>${esc(typeof x==='string'?x:x.text)}</li>`).join('')||'<li class="muted">等待下一次值得做的推进。</li>');
  stableHTML('sources',(t.sources||[]).map(s=>`<div class="source">${safeLink(s)}<br><span class="muted">${esc(s.provenance||'外部资料')} · ${esc(s.retrieved_at||'')}</span></div>`).join('')||'<p class="muted">暂时没有外部来源。</p>');
  const running=data.jobs.find(j=>j.status==='running');
  $('backgroundStatus').textContent=t.paused?'后台已暂停':running?'正在后台推进':'后台持续待命';
  $('pauseBackground').textContent=t.paused?'恢复后台':'暂停后台';
  $('nextDue').textContent=t.paused?'问题结构保留，任务不再运行':`下次检查 ${date(t.next_due_at)} · 每 ${Math.round(t.interval_seconds/60)} 分钟`;
  $('autoApply').checked=t.auto_apply;
  $('inputInbox').hidden=!data.pending_inputs?.length;
  stableHTML('pendingInputs',(data.pending_inputs||[]).map(i=>`<div class="question">${esc(i.text)}</div>`).join(''));
  $('researchNow').disabled=t.paused||Boolean(running);$('reflectNow').disabled=t.paused;
  const cards=data.candidates.slice(0,12).map(c=>{
    const p=c.proposal||c;const pending=c.status==='pending';const improvement=c.kind==='selfupdate';
    const reflection=typeof p.reflection==='string'?p.reflection:JSON.stringify(p.reflection||'');
    return {status:c.status,html:`<article class="update"><div class="meta">${esc(improvement?'工作方式复盘':'内容进展')} · ${esc(labels[c.status]||c.status)} · 基于版本 ${c.base_version} · ${date(c.created_at)}</div><h3>${esc(String(c.summary||p.summary||'本轮更新').slice(0,180))}</h3><p>${esc(reflection.slice(0,200))}</p>${p.review_reason?`<p class="review-note">${esc(p.review_reason)}</p>`:''}${improvement?`<p class="muted">建议模块：${esc(p.proposal?.module||'暂无')} · 评估：${esc(p.evaluation?.status||'未运行')} · 接受建议不会执行代码</p>`:changePreview(c,data)}<details><summary>查看候选内容与来源</summary><p>${esc(c.document?.summary||'方法改进建议，不会自动修改代码。')}</p>${(c.sources||[]).map(s=>safeLink(s)).join('<br>')}<pre>${esc(JSON.stringify(c.document||p.proposal||{},null,2))}</pre></details>${pending?`<button data-apply="${c.id}">${improvement?'接受建议（不执行代码）':'整合到当前结构'}</button><button data-reject="${c.id}">不采纳</button>`:''}</article>`};
  });
  const pendingCards=cards.filter(c=>c.status==='pending').map(c=>c.html).join('');
  const historyCards=cards.filter(c=>c.status!=='pending').map(c=>c.html).join('');
  const change=data.events.find(e=>['topic.corrected','candidate.applied'].includes(e.kind));
  const recent=change?`<p class="muted">最近结构更新 · v${t.version} · ${esc(String(change.data.note||'候选已整合，当前理解已更新。').slice(0,150))}</p>`:'';
  stableHTML('candidates',recent+(pendingCards||'<p class="muted">当前没有待审阅更新。后台会继续按约定推进。</p>')+(historyCards?`<details><summary>历史、已整合与失败记录（${cards.length-cards.filter(c=>c.status==='pending').length}）</summary><p class="muted">以下保留当时的内容，不代表当前运行状态；当前版本见上方结构。</p>${historyCards}</details>`:''));
  stableHTML('jobs',data.jobs.slice(0,15).map(j=>`<div class="job ${j.last_error?'error':''}"><div>${esc(j.kind==='selfupdate'?'方法复盘':j.kind==='organize'?'整理原话':'研究推进')} · ${esc(labels[j.status]||j.status)}<br><span class="muted">版本 ${j.base_version} · ${date(j.created_at)} ${esc(j.last_error||j.payload?.no_increment_reason||'')}</span></div>${['running','queued'].includes(j.status)?`<button data-cancel="${j.id}">取消任务</button>`:''}</div>`).join(''));
  stableHTML('evaluations',(data.evaluations||[]).map(e=>`<div class="job"><div>${esc({baseline_only:'基线已记录',checks_failed:'检查失败',checks_passed:'变更通过所列检查'}[e.status]||e.status)} · ${esc(e.checks?.join(' / '))}<br><span class="muted">${esc(e.runtime_revision)} · ${esc(e.reason)}</span></div></div>`).join('')||'<p class="muted">尚未运行方法变更检查。</p>');
  $('lastRefresh').textContent=`状态更新 ${new Date().toLocaleTimeString('zh-CN')}`;
}

async function refresh() {
  const refreshSeq=++state.refreshSeq;
  const [topics,status]=await Promise.all([api('/api/topics'),api('/api/status')]);
  if(refreshSeq!==state.refreshSeq)return;
  $('serviceStatus').textContent=status.key_configured?'持久后台已连接':'工作区可用 · 模型待配置';
  $('usage').textContent=`今日已预留：交互 ${status.usage.chat?.used||0}/${status.limits.chat} 次；后台模型 ${status.usage.llm?.used||0}/${status.limits.llm} 次；语音合成 ${status.usage.tts?.used||0}/${status.limits.tts} 计费字符；识别 ${status.usage.asr?.used||0}/${status.limits.asr} 秒。上海时间次日重置。`;
  if(!topics.some(t=>t.id===state.id))state.id=topics[0]?.id||null;
  stableHTML('topics',topics.map(t=>`<button class="topic-link ${t.id===state.id?'active':''}" data-topic="${t.id}">${esc(t.title)}<small>${t.paused?'已暂停':'持续推进'} · v${t.version}</small></button>`).join(''));
  if(state.id){const topicId=state.id;localStorage.setItem('thinking-topic',topicId);const detail=await api(`/api/topics/${topicId}`);if(refreshSeq===state.refreshSeq&&topicId===state.id)render(detail);}
}

async function selectTopic(id) {
  const selectionSeq=++state.selectionSeq;
  stopReply();voice.stopMic({discard:true});state.id=id;$('messages').innerHTML='';
  await refresh();
  if(selectionSeq!==state.selectionSeq||state.id!==id||state.data?.topic?.id!==id)return;
  const events=state.data.events.filter(e=>['user_message','assistant_message'].includes(e.kind)).reverse().slice(-12);
  const reviews=new Map(state.data.events.filter(e=>e.kind==='interaction.report_review'&&e.data.valid===false).map(e=>[e.data.report_id,e.data.reason]));
  for(const event of events)if(event.data.text){const invalid=reviews.get(event.data.report_id);addMessage(event.kind==='user_message'?'user':'assistant',invalid?`此条旧汇报已复核有误：${invalid}`:event.data.text);} 
}

$('newTopic').onclick=()=>$('newDialog').showModal();$('closeDialog').onclick=()=>$('newDialog').close();
$('newForm').onsubmit=action(async event=>{event.preventDefault();const t=await post('/api/topics',{title:$('newTitle').value,goal:$('newGoal').value});$('newDialog').close();$('newForm').reset();await selectTopic(t.id);});
$('seedTopic').onclick=action(async()=>{const t=await post('/api/topics',{title:'Thinking Helper 如何真正成立',goal:'持续激活我的思考；交互自然、延迟低；后台在我离开后继续研究与创造；把新成果接回清楚、可复利的结构；保留主体性，模块少且能持续迭代。首轮请核查目前最贴合的已有方案及其局限。'});await selectTopic(t.id);});
$('topics').onclick=action(async event=>{const button=event.target.closest('[data-topic]');if(button)await selectTopic(button.dataset.topic);});
$('chatForm').onsubmit=event=>{event.preventDefault();const text=$('chatText').value.trim();if(text)sendChat(text);};
$('report').onclick=()=>sendChat('请只根据当前工作区汇报，不假设候选已经整合。',true);
$('stopSpeech').onclick=()=>stopReply();
$('mic').onclick=action(async()=>{if(voice.isListening){voice.stopMic();return;}await voice.startMic();});
$('pauseBackground').onclick=action(async()=>{await api(`/api/topics/${state.id}`,{method:'PATCH',body:JSON.stringify({paused:!state.data.topic.paused})});await refresh();});
$('researchNow').onclick=action(async()=>{await post(`/api/topics/${state.id}/research`);await refresh();});
$('reflectNow').onclick=action(async()=>{await post(`/api/topics/${state.id}/reflect`);await refresh();});
$('saveCorrection').onclick=action(async()=>{const text=$('correction').value.trim();if(!text)return;await post(`/api/topics/${state.id}/correction`,{text,version:state.data.topic.version});$('correction').value='';await refresh();});
$('autoApply').onchange=action(async()=>{await api(`/api/topics/${state.id}`,{method:'PATCH',body:JSON.stringify({auto_apply:$('autoApply').checked})});await refresh();});
$('candidates').onclick=action(async event=>{const b=event.target.closest('[data-apply],[data-reject]');if(!b)return;if(b.dataset.apply){await post(`/api/candidates/${b.dataset.apply}/apply`);await refresh();}else{state.rejectId=b.dataset.reject;$('rejectReason').value='';$('rejectDialog').showModal();}});
$('closeReject').onclick=()=>$('rejectDialog').close();
$('rejectForm').onsubmit=action(async event=>{event.preventDefault();const reason=$('rejectKind').value+($('rejectReason').value.trim()?'：'+$('rejectReason').value.trim():'');await post(`/api/candidates/${state.rejectId}/reject`,{reason});$('rejectDialog').close();await refresh();});
$('jobs').onclick=action(async event=>{const b=event.target.closest('[data-cancel]');if(b){await post(`/api/jobs/${b.dataset.cancel}/cancel`);await refresh();}});
$('exportTopic').onclick=()=>{if(state.id)window.location.href=`/api/topics/${state.id}/export`;};
async function loadGuide(name){$('guideText').textContent=(await api(`/api/guide/${name}`)).text;}
$('showGuide').onclick=action(async()=>{$('guideDialog').showModal();await loadGuide('brief');});$('closeGuide').onclick=()=>$('guideDialog').close();
$('guideDialog').onclick=action(async event=>{const b=event.target.closest('[data-guide]');if(b)await loadGuide(b.dataset.guide);});
window.addEventListener('beforeunload',()=>{voice.close();state.controller?.abort();});
await action(refresh)();if(state.id)await action(()=>selectTopic(state.id))();
setInterval(()=>refresh().catch(error=>{$('serviceStatus').textContent='后台暂时失联，正在重连';}),4000);
