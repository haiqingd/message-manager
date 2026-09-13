const state = { token: sessionStorage.getItem('adminToken') || '', channels: [], messages: [], offset: 0, total: 0, page: 'overview' };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const names = { overview:'概览', compose:'发送测试', channels:'渠道管理', messages:'发送记录', api:'API 接入' };
const kindNames = { dingtalk:'钉钉机器人', feishu:'飞书机器人', email:'SMTP 邮件' };
const kindIcons = { dingtalk:'◈', feishu:'◇', email:'✉' };
const statusNames = { sent:'已送达', pending:'待发送', sending:'发送中', failed:'发送失败' };

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Authorization': `Bearer ${state.token}`, ...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers } });
  let data; try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}
function toast(message) { const item=$('#toast'); item.textContent=message; item.classList.remove('hidden'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>item.classList.add('hidden'),3500); }
function formatTime(value) { return new Date(value*1000).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
function overallStatus(message) { const values=message.deliveries.map(item=>item.status); return values.includes('failed')?'failed':values.every(item=>item==='sent')?'sent':values.includes('sending')?'sending':'pending'; }
function showPage(page) { state.page=page; document.querySelectorAll('.page').forEach(item=>item.classList.toggle('active',item.id===page)); document.querySelectorAll('.nav-item').forEach(item=>item.classList.toggle('active',item.dataset.page===page)); $('#current-page').textContent=names[page]; if(page==='api') renderApiExample(); else if(state.token) refresh().catch(error=>toast(`刷新失败：${error.message}`)); }
async function refresh() { const [stats, channels, messages] = await Promise.all([request('/api/admin/stats'),request('/api/admin/channels'),request(`/api/admin/messages?limit=20&offset=${state.offset}`)]); state.channels=channels; state.messages=messages.items; state.total=messages.total; renderStats(stats); renderChannels(); renderMessages(); renderDestinations(); renderApiExample(); }
function renderStats(stats) { for(const key of ['channels','messages','sent','pending','failed']) $(`#stat-${key}`).textContent=stats[key]; }
function renderChannels() {
  $('#channel-count').textContent=`${state.channels.length} 个渠道`;
  $('#channel-list').innerHTML=state.channels.length ? state.channels.map(channel=>`<div class="channel-item"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><div class="channel-meta"><strong>${escapeHtml(channel.name)}</strong><small>${kindNames[channel.kind]} · ID: ${escapeHtml(channel.id)}</small></div><span class="status ${channel.enabled?'sent':'disabled'}">${channel.enabled?'已启用':'已停用'}</span><div class="channel-actions"><button class="text-button" data-copy-id="${escapeHtml(channel.id)}">复制 ID</button><button class="text-button" data-edit="${escapeHtml(channel.id)}">编辑</button><button class="icon-button" data-delete="${escapeHtml(channel.id)}" title="删除">×</button></div></div>`).join('') : empty('还没有渠道，点击右上角添加一个。');
  $('#channel-preview').innerHTML=state.channels.length ? state.channels.slice(0,4).map(channel=>`<div class="preview-row"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><div class="row-main"><strong>${escapeHtml(channel.name)}</strong><small>${kindNames[channel.kind]}</small></div><span class="status ${channel.enabled?'sent':'disabled'}">${channel.enabled?'正常':'停用'}</span></div>`).join('') : empty('添加渠道后，这里会显示状态。');
}
function empty(text) { return `<div class="empty"><span class="empty-icon">◇</span>${text}</div>`; }
function renderMessages() {
  $('#message-count').textContent=`${state.total} 条消息`;
  $('#page-info').textContent=`第 ${Math.floor(state.offset/20)+1} 页`;
  $('#prev-page').disabled=state.offset===0; $('#next-page').disabled=state.offset+20>=state.total;
  $('#recent-messages').innerHTML=state.messages.length ? state.messages.slice(0,4).map(message=>`<div class="message-row"><span class="row-icon">↗</span><div class="row-main"><strong>${escapeHtml(message.title)}</strong><small>${escapeHtml(message.source)} · ${message.deliveries.map(d=>escapeHtml(d.channel_name)).join('、')}</small></div><span class="status ${overallStatus(message)}">${statusNames[overallStatus(message)]}</span><span class="row-time">${formatTime(message.created_at)}</span></div>`).join('') : empty('还没有发送记录。');
  $('#message-list').innerHTML=state.messages.length ? state.messages.map(message=>`<div class="message-card"><div class="message-head"><span class="row-icon">↗</span><div class="row-main"><strong>${escapeHtml(message.title)}</strong><small>${escapeHtml(message.source)} · ${formatTime(message.created_at)} · ${message.deliveries.length} 个投递</small></div><span class="status ${overallStatus(message)}">${statusNames[overallStatus(message)]}</span><span class="row-time">⌄</span></div><div class="message-details"><div class="message-body">${escapeHtml(message.body)}</div><small style="display:block;color:#a0aabc;margin:9px 0">消息 ID：${escapeHtml(message.id)}</small>${message.deliveries.map(item=>`<div class="delivery-row"><strong>${escapeHtml(item.channel_name)}</strong><small>${item.recipients.length?escapeHtml(item.recipients.join(', ')):`已尝试 ${item.attempts} 次`}</small><span class="status ${item.status}">${statusNames[item.status]}</span>${item.status==='failed'?`<button class="text-button" data-retry="${escapeHtml(item.id)}">重试</button>`:''}</div>${item.last_error?`<p class="delivery-error">${escapeHtml(item.last_error)}</p>`:''}`).join('')}</div></div>`).join('') : empty('暂无消息记录。');
}
function renderDestinations() { const enabled=state.channels.filter(c=>c.enabled); $('#destination-options').innerHTML=enabled.length?enabled.map(channel=>`<label class="destination-option"><input type="checkbox" value="${escapeHtml(channel.id)}" data-kind="${channel.kind}"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><span>${escapeHtml(channel.name)}<small>${kindNames[channel.kind]}</small></span></label>`).join(''):empty('没有可用渠道，请先添加并启用渠道。'); }
function renderApiExample() {
  const id = state.channels[0]?.id || '替换为渠道 ID';
  const payload = { source: 'order-service', title: '服务状态提醒', body: '订单服务已恢复正常。', destinations: [{ channel_id: id }], idempotency_key: 'recovered-001' };
  $('#api-example').textContent = `curl -X POST ${location.origin}/api/v1/messages -H 'Content-Type: application/json' -H 'X-API-Key: 你的 MESSAGE_API_KEY' -d '${JSON.stringify(payload, null, 2)}'`;
}

function field(label,key,type='text',placeholder='',value='',hint='') { return `<div class="field"><label for="cfg-${key}">${label}</label><input id="cfg-${key}" data-config="${key}" type="${type}" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value)}">${hint?`<small>${escapeHtml(hint)}</small>`:''}</div>`; }
function secretField(label, key, type, saved, placeholder, host = '') {
  const status = saved
    ? `<div class="saved-secret"><strong>✓ 已保存${host ? ` · ${escapeHtml(host)}` : ''}</strong><small>完整凭据不会回显。留空保留原值，输入新值可替换。</small></div>`
    : '';
  return `<div class="field"><label for="cfg-${key}">${label}</label><input id="cfg-${key}" data-config="${key}" type="${type}" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(saved ? '已保存；如需替换请输入新值' : placeholder)}">${status}</div>`;
}
function renderChannelFields(channel = state.channels.find(item => item.id === $('#channel-id').value) || null) {
  const kind = $('#channel-kind').value;
  const sameKind = channel?.kind === kind;
  const config = sameKind ? channel.config : {};
  const saved = key => sameKind && channel.configured_secrets?.includes(key);
  let html = '';
  if (sameKind && channel.configured_secrets?.length) {
    html += '<div class="secure-note">🔒 凭据已加密保存，编辑时不会显示完整内容。只需填写要修改的字段。</div>';
  }
  if (kind === 'dingtalk' || kind === 'feishu') {
    html += secretField('机器人 Webhook URL *', 'webhook_url', 'url', saved('webhook_url'), 'https://...', channel?.webhook_host || '');
    html += secretField('加签密钥（可选）', 'secret', 'password', saved('secret'), '机器人安全设置中的密钥');
  } else {
    html += field('SMTP 服务器 *', 'host', 'text', 'smtp.example.com', config.host || '');
    html += field('端口 *', 'port', 'number', '587', config.port || 587);
    html += `<div class="field"><label for="cfg-security">连接安全</label><select id="cfg-security" data-config="security"><option value="starttls" ${config.security === 'starttls' || !config.security ? 'selected' : ''}>STARTTLS</option><option value="ssl" ${config.security === 'ssl' ? 'selected' : ''}>SSL / TLS</option><option value="none" ${config.security === 'none' ? 'selected' : ''}>无加密（仅可信网络）</option></select></div>`;
    html += field('发件人邮箱 *', 'from_address', 'email', 'notify@example.com', config.from_address || '');
    html += field('SMTP 用户名', 'username', 'text', '通常为邮箱地址', config.username || '');
    html += secretField('SMTP 密码', 'password', 'password', saved('password'), '授权码或密码');
    html += field('默认收件人', 'default_recipients', 'text', 'team@example.com, ops@example.com', (config.default_recipients || []).join(', '), '发送时未指定收件人，就使用这里的地址。');
  }
  $('#channel-fields').innerHTML = html;
}

function openChannelModal(id=null) { const channel=state.channels.find(item=>item.id===id); $('#channel-id').value=id||''; $('#modal-title').textContent=channel?'编辑发送渠道':'添加发送渠道'; $('#channel-name').value=channel?.name||''; $('#channel-kind').value=channel?.kind||'dingtalk'; $('#channel-enabled').checked=channel?.enabled??true; $('#channel-error').textContent=''; renderChannelFields(channel); $('#channel-modal').classList.remove('hidden'); $('#channel-name').focus(); }
function closeChannelModal() { $('#channel-modal').classList.add('hidden'); }
async function saveChannel(event) { event.preventDefault(); const id=$('#channel-id').value; const kind=$('#channel-kind').value; const config={}; document.querySelectorAll('[data-config]').forEach(input=>{ config[input.dataset.config]=input.value.trim(); }); if(kind==='email'){ config.port=Number(config.port||587); config.default_recipients=config.default_recipients.split(',').map(x=>x.trim()).filter(Boolean); } try { await request(id?`/api/admin/channels/${id}`:'/api/admin/channels',{method:id?'PUT':'POST',body:JSON.stringify({name:$('#channel-name').value.trim(),kind,enabled:$('#channel-enabled').checked,config})}); closeChannelModal(); await refresh(); toast(id?'渠道已更新':'渠道已添加'); } catch(error) { $('#channel-error').textContent=error.message; } }
async function watchDelivery(messageId, result) {
  for (let attempt = 0; attempt < 45; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    let message;
    try { message = await request(`/api/admin/messages/${messageId}`); }
    catch (error) { result.textContent = `状态查询失败：${error.message}；消息 ID：${messageId}`; result.className = 'inline-message error'; return; }
    const deliveries = message.deliveries;
    const failed = deliveries.filter(item => item.status === 'failed');
    if (failed.length) {
      result.textContent = `发送失败：${failed.map(item => `${item.channel_name}：${item.last_error || '未知错误'}`).join('；')}。消息 ID：${messageId}`;
      result.className = 'inline-message error';
      await refresh();
      return;
    }
    if (deliveries.every(item => item.status === 'sent')) {
      result.textContent = `发送成功，消息 ID：${messageId}`;
      result.className = 'inline-message success';
      await refresh();
      return;
    }
    const retrying = deliveries.find(item => item.last_error);
    if (retrying) result.textContent = `正在重试 ${retrying.channel_name}：${retrying.last_error}。消息 ID：${messageId}`;
  }
  result.textContent = `仍在后台发送，请到「发送记录」查看最终结果。消息 ID：${messageId}`;
  await refresh();
}
async function sendMessage(event) { event.preventDefault(); const checked=[...document.querySelectorAll('#destination-options input:checked')]; const result=$('#send-result'); result.className='inline-message'; if(!checked.length){result.textContent='请至少选择一个发送渠道。';result.classList.add('error');return;} const recipients=$('#email-recipients').value.split(',').map(x=>x.trim()).filter(Boolean); const destinations=checked.map(input=>({channel_id:input.value,recipients:input.dataset.kind==='email'?recipients:[]})); try{const data=await request('/api/admin/messages',{method:'POST',body:JSON.stringify({source:'console',title:$('#send-title').value.trim(),body:$('#send-body').value.trim(),destinations})}); result.textContent=`已加入队列，正在等待发送结果。消息 ID：${data.id}`;result.classList.add('success');$('#send-form').reset();await refresh();watchDelivery(data.id, result).catch(error => { result.textContent=`状态查询失败：${error.message}；消息 ID：${data.id}`; result.className='inline-message error'; });}catch(error){result.textContent=error.message;result.classList.add('error');} }
async function authenticate(token) { state.token=token; await request('/api/admin/session'); sessionStorage.setItem('adminToken',token); $('#login').classList.add('hidden'); $('#shell').classList.remove('hidden'); await refresh(); }

$('#today').textContent=new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric'});
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();$('#login-error').textContent='';try{await authenticate($('#admin-token').value.trim());}catch(error){$('#login-error').textContent='令牌无效或服务不可用。';}});
$('#logout').addEventListener('click',()=>{sessionStorage.removeItem('adminToken');state.token='';$('#shell').classList.add('hidden');$('#login').classList.remove('hidden');$('#admin-token').value='';});
$('#nav').addEventListener('click',event=>{const button=event.target.closest('[data-page]');if(button)showPage(button.dataset.page);});
document.body.addEventListener('click',event=>{const go=event.target.closest('[data-go]');if(go)showPage(go.dataset.go);});
$('#add-channel').addEventListener('click',()=>openChannelModal()); $('#close-modal').addEventListener('click',closeChannelModal);$('#cancel-modal').addEventListener('click',closeChannelModal);$('#channel-modal').addEventListener('click',event=>{if(event.target.id==='channel-modal')closeChannelModal();});
$('#channel-kind').addEventListener('change',()=>renderChannelFields());$('#channel-form').addEventListener('submit',saveChannel);$('#send-form').addEventListener('submit',sendMessage);
$('#destination-options').addEventListener('change',()=>{$('#email-recipients-wrap').classList.toggle('hidden',!document.querySelector('#destination-options input[data-kind="email"]:checked'));});
$('#channel-list').addEventListener('click',async event=>{const edit=event.target.closest('[data-edit]');const remove=event.target.closest('[data-delete]');const copy=event.target.closest('[data-copy-id]');if(edit)openChannelModal(edit.dataset.edit);if(copy){await navigator.clipboard.writeText(copy.dataset.copyId);toast('渠道 ID 已复制');}if(remove){const channel=state.channels.find(c=>c.id===remove.dataset.delete);if(!confirm(`删除渠道「${channel.name}」？已有发送记录的渠道无法删除，可改为停用。`))return;try{await request(`/api/admin/channels/${channel.id}`,{method:'DELETE'});await refresh();toast('渠道已删除');}catch(error){toast(error.message);}}});
$('#message-list').addEventListener('click',async event=>{const retry=event.target.closest('[data-retry]');if(retry){event.stopPropagation();try{await request(`/api/admin/deliveries/${retry.dataset.retry}/retry`,{method:'POST'});await refresh();toast('已重新加入队列');}catch(error){toast(error.message);}return;}const head=event.target.closest('.message-head');if(head)head.parentElement.classList.toggle('expanded');});
$('#refresh-messages').addEventListener('click',async()=>{await refresh();toast('记录已更新');});$('#prev-page').addEventListener('click',async()=>{state.offset=Math.max(0,state.offset-20);await refresh();});$('#next-page').addEventListener('click',async()=>{state.offset+=20;await refresh();});$('#copy-example').addEventListener('click',async()=>{await navigator.clipboard.writeText($('#api-example').textContent);toast('示例已复制');});
if(state.token)authenticate(state.token).catch(()=>{sessionStorage.removeItem('adminToken');state.token='';});
