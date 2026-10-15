const state = { token: sessionStorage.getItem('adminToken') || '', channels: [], messages: [], routes: [], subscriptions: [], qqEvents: [], offset: 0, total: 0, page: 'overview' };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const names = { overview:'概览', compose:'发送测试', channels:'渠道管理', subscriptions:'订阅管理', qqcb:'QQ 回调', messages:'发送记录', api:'API 接入' };
const kindNames = { dingtalk:'钉钉机器人', feishu:'飞书机器人', qq:'QQ 机器人', email:'SMTP 邮件' };
const kindIcons = { dingtalk:'◈', feishu:'◇', qq:'Q', email:'✉' };
const statusNames = { sent:'已送达', pending:'待发送', sending:'发送中', failed:'发送失败' };
const qqEventNames = { '*':'任意事件', GROUP_AT_MESSAGE_CREATE:'群 @ 消息', GROUP_ADD_ROBOT:'机器人进群', C2C_MESSAGE_CREATE:'私聊消息', FRIEND_ADD:'好友添加' };
const qqChatNames = { any:'不限', group:'群聊', user:'私聊' };
const qqActionNames = { channel:'创建渠道', forward:'转发插槽', log:'仅记录', party:'组队游戏' };

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
async function refresh() { const [stats, channels, messages, routes, qqEvents, subscriptions] = await Promise.all([request('/api/admin/stats'),request('/api/admin/channels'),request(`/api/admin/messages?limit=20&offset=${state.offset}`),request('/api/admin/qq/routes'),request('/api/admin/qq/events?limit=50'),request('/api/admin/subscriptions')]); state.channels=channels; state.messages=messages.items; state.total=messages.total; state.routes=routes; state.qqEvents=qqEvents.items; state.subscriptions=subscriptions; renderStats(stats); renderChannels(); renderMessages(); renderDestinations(); renderRoutes(); renderQqEvents(); renderSubscriptions(); renderApiExample(); }
function renderStats(stats) { for(const key of ['channels','messages','sent','pending','failed']) $(`#stat-${key}`).textContent=stats[key]; }
function renderChannels() {
  $('#channel-count').textContent=`${state.channels.length} 个渠道`;
  $('#channel-list').innerHTML=state.channels.length ? state.channels.map(channel=>`<div class="channel-item"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><div class="channel-meta"><strong>${escapeHtml(channel.name)}</strong><small>${kindNames[channel.kind]} · ID: ${escapeHtml(channel.id)}</small></div><span class="status ${channel.enabled?'sent':'disabled'}">${channel.enabled?'已启用':'已停用'}</span><div class="channel-actions"><button class="text-button" data-copy-id="${escapeHtml(channel.id)}">复制 ID</button><button class="text-button" data-edit="${escapeHtml(channel.id)}">编辑</button><button class="icon-button" data-delete="${escapeHtml(channel.id)}" title="删除">×</button></div></div>`).join('') : empty('还没有渠道，点击右上角添加一个。');
  $('#channel-preview').innerHTML=state.channels.length ? state.channels.slice(0,4).map(channel=>`<div class="preview-row"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><div class="row-main"><strong>${escapeHtml(channel.name)}</strong><small>${kindNames[channel.kind]}</small></div><span class="status ${channel.enabled?'sent':'disabled'}">${channel.enabled?'正常':'停用'}</span></div>`).join('') : empty('添加渠道后，这里会显示状态。');
}
function empty(text) { return `<div class="empty"><span class="empty-icon">◇</span>${text}</div>`; }
function renderSubscriptions() {
  $('#subscription-count').textContent = `${state.subscriptions.length} 个订阅`;
  $('#subscription-list').innerHTML = state.subscriptions.length ? state.subscriptions.map(item => {
    const targets = item.destinations.map(destination => state.channels.find(channel => channel.id === destination.channel_id)?.name || '渠道已移除').join('、');
    const source = item.kind === 'imap' ? `${item.config.provider === 'gmail' ? 'Gmail' : 'QQ 邮箱'} · ${item.config.username}` : item.kind === 'x' ? `X · @${item.config.username}` : `RSS · ${item.feed_host || ''}`;
    const filters = item.kind === 'imap' ? `标题：${(item.config.subject_keywords || []).join(' / ') || '不限'}；发件人：${(item.config.senders || []).join(' / ') || '不限'} · ${item.config.match_mode === 'all' ? '全部满足' : '任一满足'}` : '';
    return `<div class="subscription-item"><div class="channel-meta"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(source)} → ${escapeHtml(targets)}</small>${filters ? `<small>${escapeHtml(filters)}</small>` : ''}<small>每 ${item.poll_interval} 秒 · 最近检查：${item.last_checked_at ? formatTime(item.last_checked_at) : '尚未检查'}${item.enabled ? ` · 下次：${formatTime(item.next_poll_at)}` : ''}</small><small>${item.last_success_at ? `窗口起点（上次成功检查）：${formatTime(item.last_success_at)}` : '首次检查将建立时间基线'}</small>${item.last_error ? `<p class="delivery-error">${escapeHtml(item.last_error)}</p>` : ''}</div><span class="status ${!item.enabled ? 'disabled' : item.last_error ? 'failed' : 'sent'}">${!item.enabled ? '已停用' : item.last_error ? '检查失败' : '已启用'}</span><div class="channel-actions"><button class="text-button" data-check-subscription="${item.id}" ${!item.enabled ? 'disabled' : ''}>立即检查</button><button class="text-button" data-edit-subscription="${item.id}">编辑</button><button class="icon-button" data-delete-subscription="${item.id}" title="删除">×</button></div></div>`;
  }).join('') : empty('添加一个 X 账号、RSS 源或邮箱，开始自动转发新内容。');
}
function renderSubscriptionFields(item) {
  const kind = $('#subscription-kind').value;
  const config = item?.kind === kind ? item.config : {};
  const saved = key => item?.kind === kind && item.configured_secrets?.includes(key);
  if (kind === 'imap') {
    $('#subscription-fields').innerHTML = `<div class="field"><label for="subscription-provider">邮箱服务</label><select id="subscription-provider"><option value="qq" ${config.provider === 'gmail' ? '' : 'selected'}>QQ 邮箱</option><option value="gmail" ${config.provider === 'gmail' ? 'selected' : ''}>Gmail</option></select><small>通过 SSL/TLS 读取收件箱，不修改已读状态，不下载或转发附件。</small></div><div class="field"><label for="subscription-mail-user">邮箱地址 *</label><input id="subscription-mail-user" type="email" value="${escapeHtml(config.username || '')}" placeholder="name@qq.com 或 name@gmail.com" autocomplete="off" required></div><div class="field"><label for="subscription-mail-password">授权码 / 应用专用密码 *</label><input id="subscription-mail-password" type="password" autocomplete="new-password" placeholder="${saved('password') ? '已加密保存；留空保留原凭据' : 'QQ 授权码或 Gmail 应用专用密码'}" ${saved('password') ? '' : 'required'}><small>QQ：开启 IMAP/SMTP 后生成授权码。Gmail：开启两步验证后生成应用专用密码；部分受管理账号可能不可用。<a href="https://service.mail.qq.com/detail/0/339" target="_blank" rel="noopener">QQ 官方说明 ↗</a> · <a href="https://support.google.com/accounts/answer/185833?hl=zh-Hans" target="_blank" rel="noopener">Google 官方说明 ↗</a></small></div><div class="field"><label for="subscription-mail-subjects">标题包含</label><textarea id="subscription-mail-subjects" rows="3" placeholder="每行一个关键词，例如：账单、服务通知">${escapeHtml((config.subject_keywords || []).join('\n'))}</textarea><small>每行一项，命中其中任意关键词即可；忽略大小写。</small></div><div class="field"><label for="subscription-mail-senders">发件人匹配</label><textarea id="subscription-mail-senders" rows="3" placeholder="每行一个邮箱地址或发件人名称关键词">${escapeHtml((config.senders || []).join('\n'))}</textarea><small>邮箱地址精确匹配，名称按包含匹配；每行一项，忽略大小写。标题或发件人至少填写一类。</small></div><div class="field"><label for="subscription-mail-match">条件组合</label><select id="subscription-mail-match"><option value="any" ${config.match_mode === 'all' ? '' : 'selected'}>任一类条件满足（标题或发件人）</option><option value="all" ${config.match_mode === 'all' ? 'selected' : ''}>所有已填写的条件都满足</option></select><small>未填写的条件不参与匹配；修改过滤条件后，对下次检查时间窗口内的新邮件生效。</small></div>`;
  } else if (kind === 'x') {
    $('#subscription-fields').innerHTML = `<div class="field"><label for="subscription-username">X 账号 *</label><input id="subscription-username" value="${escapeHtml(config.username || '')}" placeholder="@username 或 https://x.com/username" required></div><div class="field"><label for="subscription-token">Bearer Token *</label><input id="subscription-token" type="password" autocomplete="off" placeholder="${saved('bearer_token') ? '已加密保存；留空保留原凭据' : 'X 开发者平台的 Bearer Token'}" ${saved('bearer_token') ? '' : 'required'}><small>X 官方 API 按用量计费，账号需有相应读取权限和可用额度。</small></div><div class="checkbox-row"><label><input id="subscription-replies" type="checkbox" ${config.include_replies ? 'checked' : ''}> 包含回复</label> <label><input id="subscription-reposts" type="checkbox" ${config.include_reposts ? 'checked' : ''}> 包含转帖</label></div>`;
  } else {
    $('#subscription-fields').innerHTML = `<div class="field"><label for="subscription-url">RSS / Atom 地址 *</label><input id="subscription-url" type="url" autocomplete="off" placeholder="${saved('feed_url') ? '已加密保存；留空保留原地址' : 'https://your-rss-service.example/feed'}" ${saved('feed_url') ? '' : 'required'}><small>${saved('feed_url') ? `已配置主机：${escapeHtml(item.feed_host)}。` : ''}X 账号需由第三方服务生成 RSS；请填写已验证可用的源地址。</small></div>`;
  }
}
function openSubscriptionModal(id = null) {
  const item = state.subscriptions.find(subscription => subscription.id === id);
  $('#subscription-id').value = id || '';
  $('#subscription-modal-title').textContent = item ? '编辑订阅' : '添加订阅';
  $('#subscription-name').value = item?.name || '';
  $('#subscription-kind').value = item?.kind || 'x';
  $('#subscription-interval').value = item?.poll_interval || 300;
  $('#subscription-enabled').checked = item?.enabled ?? true;
  $('#subscription-error').textContent = '';
  renderSubscriptionFields(item);
  const available = state.channels.filter(channel => channel.enabled || item?.destinations.some(destination => destination.channel_id === channel.id));
  $('#subscription-destinations').innerHTML = available.length ? available.map(channel => {
    const destination = item?.destinations.find(target => target.channel_id === channel.id);
    return `<div><label class="destination-option"><input type="checkbox" value="${channel.id}" data-kind="${channel.kind}" ${destination ? 'checked' : ''}><span>${escapeHtml(channel.name)}${channel.enabled ? '' : '（已停用）'}</span></label>${channel.kind === 'email' ? `<input data-subscription-recipients="${channel.id}" value="${escapeHtml((destination?.recipients || []).join(', '))}" placeholder="收件人；留空使用渠道默认地址" aria-label="${escapeHtml(channel.name)} 收件人">` : ''}</div>`;
  }).join('') : empty('请先添加并启用发送渠道。');
  $('#subscription-modal').classList.remove('hidden');
  $('#subscription-name').focus();
}
function closeSubscriptionModal() { $('#subscription-modal').classList.add('hidden'); }
async function saveSubscription(event) {
  event.preventDefault();
  const id = $('#subscription-id').value;
  const kind = $('#subscription-kind').value;
  let config;
  if (kind === 'imap') {
    const lines = selector => $(selector).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    config = {provider: $('#subscription-provider').value, username: $('#subscription-mail-user').value.trim(), password: $('#subscription-mail-password').value.trim(), subject_keywords: lines('#subscription-mail-subjects'), senders: lines('#subscription-mail-senders'), match_mode: $('#subscription-mail-match').value};
    if (!config.subject_keywords.length && !config.senders.length) { $('#subscription-error').textContent = '请至少填写一个标题关键词或发件人条件。'; return; }
  } else if (kind === 'x') {
    config = {username: $('#subscription-username').value.trim(), bearer_token: $('#subscription-token').value.trim(), include_replies: $('#subscription-replies').checked, include_reposts: $('#subscription-reposts').checked};
  } else { config = {feed_url: $('#subscription-url').value.trim()}; }
  const destinations = [...document.querySelectorAll('#subscription-destinations input[type="checkbox"]:checked')].map(input => ({channel_id: input.value, recipients: input.dataset.kind === 'email' ? document.querySelector(`[data-subscription-recipients="${input.value}"]`).value.split(',').map(value => value.trim()).filter(Boolean) : []}));
  if (!destinations.length) { $('#subscription-error').textContent = '请至少选择一个转发渠道。'; return; }
  try {
    await request(id ? `/api/admin/subscriptions/${id}` : '/api/admin/subscriptions', {method: id ? 'PUT' : 'POST', body: JSON.stringify({name: $('#subscription-name').value.trim(), kind, enabled: $('#subscription-enabled').checked, config, destinations, poll_interval: Number($('#subscription-interval').value)})});
    closeSubscriptionModal(); await refresh(); toast(id ? '订阅已更新' : '订阅已添加；首次检查将建立基线');
  } catch (error) { $('#subscription-error').textContent = error.message; }
}
function renderRoutes() {
  $('#route-count').textContent=`${state.routes.length} 条规则`;
  $('#route-list').innerHTML=state.routes.length ? state.routes.map(route=>`<div class="channel-item"><div class="channel-meta"><strong>${escapeHtml(route.name)}</strong><small>${route.event_types.map(t=>qqEventNames[t]||t).join('、')} · ${qqChatNames[route.chat_type]}${route.openid?` · ${escapeHtml(route.openid)}`:''} → ${qqActionNames[route.action]}${route.action==='forward'?(route.configured_secrets?.includes('webhook_url')?'':'（未配置 URL）'):''}</small></div><span class="status ${route.enabled?'sent':'disabled'}">${route.enabled?'启用':'停用'}</span><div class="channel-actions"><button class="text-button" data-edit-route="${escapeHtml(route.id)}">编辑</button><button class="icon-button" data-delete-route="${escapeHtml(route.id)}" title="删除">×</button></div></div>`).join('') : empty('还没有路由规则；所有事件只会记录，不会触发任何插槽。');
}
function renderQqEvents() {
  $('#qq-event-list').innerHTML=state.qqEvents.length ? state.qqEvents.map(event=>`<div class="message-card"><div class="message-head"><span class="row-icon">◈</span><div class="row-main"><strong>${escapeHtml(qqEventNames[event.event_type]||event.event_type)}</strong><small>${formatTime(event.received_at)} · App ${escapeHtml(event.app_id)}${event.openid?` · ${event.chat_type==='user'?'用户':'群'} ${escapeHtml(event.openid.slice(0,12))}…`:''}${event.payload?.d?.content?` · ${escapeHtml(String(event.payload.d.content).trim().slice(0,30))}`:''}</small></div><span class="status ${event.status==='ok'?'sent':event.status==='error'?'failed':'pending'}">${qqActionNames[event.action]||event.action} · ${event.status==='ok'?'成功':event.status==='error'?'失败':event.status}</span><span class="row-time">⌄</span></div><div class="message-details"><div class="message-body"><pre>${escapeHtml(JSON.stringify(event.payload,null,2))}</pre></div>${event.detail?`<p class="delivery-error">${escapeHtml(event.detail)}</p>`:''}</div></div>`).join('') : empty('还没有收到回调事件。在 QQ 平台配置回调地址并订阅事件后，这里会记录每一个请求。');
}
function renderMessages() {
  $('#message-count').textContent=`${state.total} 条消息`;
  $('#page-info').textContent=`第 ${Math.floor(state.offset/20)+1} 页`;
  $('#prev-page').disabled=state.offset===0; $('#next-page').disabled=state.offset+20>=state.total;
  $('#recent-messages').innerHTML=state.messages.length ? state.messages.slice(0,4).map(message=>`<div class="message-row"><span class="row-icon">↗</span><div class="row-main"><strong>${escapeHtml(message.title)}</strong><small>${escapeHtml(message.source)} · ${message.deliveries.map(d=>escapeHtml(d.channel_name)).join('、')}</small></div><span class="status ${overallStatus(message)}">${statusNames[overallStatus(message)]}</span><span class="row-time">${formatTime(message.created_at)}</span></div>`).join('') : empty('还没有发送记录。');
  $('#message-list').innerHTML=state.messages.length ? state.messages.map(message=>`<div class="message-card"><div class="message-head"><span class="row-icon">↗</span><div class="row-main"><strong>${escapeHtml(message.title)}</strong><small>${escapeHtml(message.source)} · ${formatTime(message.created_at)} · ${message.deliveries.length} 个投递</small></div><span class="status ${overallStatus(message)}">${statusNames[overallStatus(message)]}</span><span class="row-time">⌄</span></div><div class="message-details"><div class="message-body">${escapeHtml(message.body)}</div><small style="display:block;color:#a0aabc;margin:9px 0">消息 ID：${escapeHtml(message.id)}</small>${message.deliveries.map(item=>`<div class="delivery-row"><strong>${escapeHtml(item.channel_name)}</strong><small>${item.recipients.length?escapeHtml(item.recipients.join(', ')):`已尝试 ${item.attempts} 次`}</small><span class="status ${item.status}">${statusNames[item.status]}</span>${item.status==='failed'?`<button class="text-button" data-retry="${escapeHtml(item.id)}">重试</button>`:''}</div>${item.last_error?`<p class="delivery-error">${escapeHtml(item.last_error)}</p>`:''}`).join('')}</div></div>`).join('') : empty('暂无消息记录。');
}
function renderDestinations() { const enabled=state.channels.filter(c=>c.enabled); $('#destination-options').innerHTML=enabled.length?enabled.map(channel=>`<label class="destination-option"><input type="checkbox" value="${escapeHtml(channel.id)}" data-kind="${channel.kind}"><span class="channel-icon ${channel.kind}">${kindIcons[channel.kind]}</span><span>${escapeHtml(channel.name)}<small>${kindNames[channel.kind]}</small></span></label>`).join(''):empty('没有可用渠道，请先添加并启用渠道。'); }
function renderApiExample() {
  const id = state.channels.find(channel => channel.enabled && (channel.kind !== 'qq' || channel.config?.group_openid))?.id || '替换为渠道 ID';
  const payload = { source: 'order-service', title: '服务状态提醒', body: '订单服务已恢复正常。', destinations: [{ channel_id: id }], idempotency_key: 'recovered-001' };
  $('#api-channels-example').textContent = `curl ${location.origin}/api/v1/channels -H 'X-API-Key: 你的 MESSAGE_API_KEY'`;
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
  } else if (kind === 'qq') {
    html += field('机器人 AppID *', 'app_id', 'text', 'QQ 开放平台中的 AppID', config.app_id || '');
    html += secretField('机器人 AppSecret *', 'client_secret', 'password', saved('client_secret'), 'QQ 开放平台中的 AppSecret');
    html += `<div class="field"><label for="cfg-chat_type">聊天类型</label><select id="cfg-chat_type" data-config="chat_type"><option value="group" ${config.chat_type === 'user' ? '' : 'selected'}>群聊（群 OpenID）</option><option value="user" ${config.chat_type === 'user' ? 'selected' : ''}>私聊（用户 OpenID）</option></select></div>`;
    html += field('目标 OpenID（可留空）', 'group_openid', 'text', '群消息事件中的 group_openid 或私聊事件中的 user_openid', config.group_openid || '', '留空时该渠道仅作为机器人身份，用于回调验签和给自动创建的渠道提供凭据；回调收到群聊/私聊消息后会按「QQ 回调」页的路由规则自动创建渠道。');
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
function renderRouteEventTypes(selected=['*']) { $('#route-event-types').innerHTML=Object.entries(qqEventNames).map(([value,label])=>`<label class="destination-option"><input type="checkbox" value="${value}" ${selected.includes(value)?'checked':''}><span>${escapeHtml(label)}</span></label>`).join(''); }
function openRouteModal(id=null) { const route=state.routes.find(item=>item.id===id); $('#route-id').value=id||''; $('#route-modal-title').textContent=route?'编辑路由规则':'添加路由规则'; $('#route-name').value=route?.name||''; renderRouteEventTypes(route?.event_types||['*']); $('#route-chat-type').value=route?.chat_type||'any'; $('#route-openid').value=route?.openid||''; $('#route-action').value=route?.action||'channel'; $('#route-webhook').value=''; $('#route-md').value=route?.config?.markdown_template_id||''; $('#route-enabled').checked=route?.enabled??true; $('#route-error').textContent=''; $('#route-webhook-wrap').classList.toggle('hidden',$('#route-action').value!=='forward'); $('#route-md-wrap').classList.toggle('hidden',$('#route-action').value!=='party'); $('#route-modal').classList.remove('hidden'); $('#route-name').focus(); }
function closeRouteModal() { $('#route-modal').classList.add('hidden'); }
async function saveRoute(event) { event.preventDefault(); const id=$('#route-id').value; const event_types=[...document.querySelectorAll('#route-event-types input:checked')].map(input=>input.value); const config={}; if($('#route-action').value==='forward') config.webhook_url=$('#route-webhook').value.trim(); if($('#route-action').value==='party') config.markdown_template_id=$('#route-md').value.trim(); try { await request(id?`/api/admin/qq/routes/${id}`:'/api/admin/qq/routes',{method:id?'PUT':'POST',body:JSON.stringify({name:$('#route-name').value.trim(),enabled:$('#route-enabled').checked,event_types,chat_type:$('#route-chat-type').value,openid:$('#route-openid').value.trim(),action:$('#route-action').value,config})}); closeRouteModal(); await refresh(); toast(id?'规则已更新':'规则已添加'); } catch(error) { $('#route-error').textContent=error.message; } }
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
$('#add-route').addEventListener('click',()=>openRouteModal());$('#close-route-modal').addEventListener('click',closeRouteModal);$('#cancel-route-modal').addEventListener('click',closeRouteModal);$('#route-modal').addEventListener('click',event=>{if(event.target.id==='route-modal')closeRouteModal();});$('#route-form').addEventListener('submit',saveRoute);$('#route-action').addEventListener('change',()=>{$('#route-webhook-wrap').classList.toggle('hidden',$('#route-action').value!=='forward');$('#route-md-wrap').classList.toggle('hidden',$('#route-action').value!=='party');});
$('#route-list').addEventListener('click',async event=>{const edit=event.target.closest('[data-edit-route]');const remove=event.target.closest('[data-delete-route]');if(edit)openRouteModal(edit.dataset.editRoute);if(remove){const route=state.routes.find(item=>item.id===remove.dataset.deleteRoute);if(!confirm(`删除路由规则「${route.name}」？`))return;try{await request(`/api/admin/qq/routes/${route.id}`,{method:'DELETE'});await refresh();toast('规则已删除');}catch(error){toast(error.message);}}});
$('#refresh-qq-events').addEventListener('click',async()=>{await refresh();toast('事件记录已更新');});
$('#clear-qq-events').addEventListener('click',async()=>{if(!confirm('清空全部 QQ 回调事件记录？此操作不可恢复。'))return;try{const result=await request('/api/admin/qq/events',{method:'DELETE'});await refresh();toast(`已清空 ${result.cleared} 条记录`);}catch(error){toast(error.message);}});
$('#qq-event-list').addEventListener('click',event=>{const head=event.target.closest('.message-head');if(head)head.parentElement.classList.toggle('expanded');});
$('#destination-options').addEventListener('change',()=>{$('#email-recipients-wrap').classList.toggle('hidden',!document.querySelector('#destination-options input[data-kind="email"]:checked'));});
$('#channel-list').addEventListener('click',async event=>{const edit=event.target.closest('[data-edit]');const remove=event.target.closest('[data-delete]');const copy=event.target.closest('[data-copy-id]');if(edit)openChannelModal(edit.dataset.edit);if(copy){await navigator.clipboard.writeText(copy.dataset.copyId);toast('渠道 ID 已复制');}if(remove){const channel=state.channels.find(c=>c.id===remove.dataset.delete);if(!confirm(`删除渠道「${channel.name}」？已有发送记录的渠道无法删除，可改为停用。`))return;try{await request(`/api/admin/channels/${channel.id}`,{method:'DELETE'});await refresh();toast('渠道已删除');}catch(error){toast(error.message);}}});
$('#message-list').addEventListener('click',async event=>{const retry=event.target.closest('[data-retry]');if(retry){event.stopPropagation();try{await request(`/api/admin/deliveries/${retry.dataset.retry}/retry`,{method:'POST'});await refresh();toast('已重新加入队列');}catch(error){toast(error.message);}return;}const head=event.target.closest('.message-head');if(head)head.parentElement.classList.toggle('expanded');});
$('#refresh-messages').addEventListener('click',async()=>{await refresh();toast('记录已更新');});$('#prev-page').addEventListener('click',async()=>{state.offset=Math.max(0,state.offset-20);await refresh();});$('#next-page').addEventListener('click',async()=>{state.offset+=20;await refresh();});$('#copy-example').addEventListener('click',async()=>{await navigator.clipboard.writeText($('#api-example').textContent);toast('示例已复制');});
$('#copy-channels-example').addEventListener('click',async()=>{await navigator.clipboard.writeText($('#api-channels-example').textContent);toast('渠道列表示例已复制');});
$('#add-subscription').addEventListener('click', () => openSubscriptionModal());
$('#close-subscription-modal').addEventListener('click', closeSubscriptionModal);
$('#cancel-subscription-modal').addEventListener('click', closeSubscriptionModal);
$('#subscription-modal').addEventListener('click', event => { if (event.target.id === 'subscription-modal') closeSubscriptionModal(); });
$('#subscription-form').addEventListener('submit', saveSubscription);
$('#subscription-kind').addEventListener('change', () => renderSubscriptionFields(state.subscriptions.find(item => item.id === $('#subscription-id').value)));
$('#refresh-subscriptions').addEventListener('click', () => refresh().catch(error => toast(error.message)));
$('#subscription-list').addEventListener('click', async event => {
  const edit = event.target.closest('[data-edit-subscription]');
  const remove = event.target.closest('[data-delete-subscription]');
  const check = event.target.closest('[data-check-subscription]');
  if (edit) openSubscriptionModal(edit.dataset.editSubscription);
  try {
    if (remove) {
      const item = state.subscriptions.find(subscription => subscription.id === remove.dataset.deleteSubscription);
      if (!confirm(`删除订阅「${item.name}」？已生成的发送记录会保留。`)) return;
      await request(`/api/admin/subscriptions/${item.id}`, {method: 'DELETE'});
      await refresh(); toast('订阅已删除');
    }
    if (check) {
      check.disabled = true; check.textContent = '检查中…';
      const result = await request(`/api/admin/subscriptions/${check.dataset.checkSubscription}/check`, {method: 'POST'});
      await refresh();
      toast(result.error || (result.baseline ? '已建立基线，之后将转发新内容' : `检查完成，${result.queued} 条新内容已加入发送队列`));
    }
  } catch (error) { toast(error.message); }
  finally { if (check) { check.disabled = false; check.textContent = '立即检查'; } }
});
if(state.token)authenticate(state.token).catch(()=>{sessionStorage.removeItem('adminToken');state.token='';});
