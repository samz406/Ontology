import { FormEvent, useCallback, useEffect, useState } from 'react'

type Me = { tenant_id: string; actor_id: string; role: string }
type Source = { id: string; name: string; last_successful_sync_at?: string | null }
type Property = { name: string; sensitivity: string; authority_source_system_id: string | null }
type Entity = { id: string; display_name: string; visibility: string }
type Rule = { id: string; name: string; version: number; status: string }
type SyncTask = { id: string; state: string; error_code: string | null }
type AdminState = { active_bundle_id: string | null; sources: Source[]; properties: Property[]; entities: Entity[]; rules: Rule[]; tasks: SyncTask[] }
type History = { id: string; decision: string; execution_state: string; created_at: string; entity_name: string }
type Evidence = { id: string; predicate: string; observed_value: unknown; source_record_key: string; source_version: number; observed_at: string }
type Result = {
  investigation_id: string; decision: { kind: string; reason_codes: string[] };
  execution: { state: string }; quality: { freshness: string; source_completeness: string; missing_required_facts: string[] };
  semantic_bundle_id: string; identity_epoch: number; evidence_ids: string[];
  rule_trace: unknown; sources: { source_system_id: string; last_successful_sync_at: string | null }[];
  display_message: string;
}
type Tab = 'investigate' | 'ontology' | 'operations'

const API = import.meta.env.VITE_API_URL || '/api'
const defaultRule = JSON.stringify({ all: [
  { fact: 'payment.status', op: 'eq', value: 'SETTLED' },
  { fact: 'shipment.status', op: 'eq', value: 'NOT_SHIPPED' },
] }, null, 2)

async function call<T>(token: string, path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`)
  return data as T
}

function short(id?: string | null) { return id ? `${id.slice(0, 8)}…` : '—' }
function formatDate(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '暂无' }
function Badge({ value }: { value: string }) {
  const tone = value === 'ELIGIBLE' || value === 'COMPLETED' || value === 'DONE' || value === 'PUBLISHED'
    ? 'green' : value === 'CONFLICT' || value === 'FAILED' || value === 'INELIGIBLE' ? 'red' : 'amber'
  return <span className={`badge ${tone}`}>{value}</span>
}

export default function App() {
  const [token, setToken] = useState(sessionStorage.getItem('ontology_token') || '')
  const [tokenInput, setTokenInput] = useState('')
  const [me, setMe] = useState<Me | null>(null)
  const [sources, setSources] = useState<Source[]>([])
  const [admin, setAdmin] = useState<AdminState | null>(null)
  const [history, setHistory] = useState<History[]>([])
  const [tab, setTab] = useState<Tab>('investigate')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [orderSource, setOrderSource] = useState('')
  const [orderKey, setOrderKey] = useState('ORDER-10001')
  const [question, setQuestion] = useState('现在符合退款资格吗？')
  const [result, setResult] = useState<Result | null>(null)
  const [evidence, setEvidence] = useState<Record<string, Evidence>>({})
  const [adminMode, setAdminMode] = useState('source')
  const [newToken, setNewToken] = useState('')
  const [form, setForm] = useState<Record<string, string>>({
    sourceName: '', maxAge: '3600', entityName: '', visibility: 'shared', propertyName: '',
    sensitivity: 'internal', sourceId: '', entityId: '', recordKey: '', sourceField: 'status',
    targetProperty: '', ruleName: 'refund_eligibility', version: '1',
    requiredFacts: 'payment.status,shipment.status', expression: defaultRule,
    eventVersion: '2', eventValue: '',
  })
  const setField = (key: string, value: string) => setForm(old => ({ ...old, [key]: value }))

  const refresh = useCallback(async (auth: string) => {
    const user = await call<Me>(auth, '/v1/me')
    setMe(user)
    const [listedSources, listedHistory] = await Promise.all([
      call<Source[]>(auth, '/v1/sources'), call<History[]>(auth, '/v1/investigations'),
    ])
    setSources(listedSources)
    setHistory(listedHistory)
    if (user.role === 'admin') setAdmin(await call<AdminState>(auth, '/v1/admin/state'))
    else setAdmin(null)
  }, [])

  useEffect(() => {
    if (!token) return
    refresh(token).catch(err => { setMe(null); setError(err.message) })
  }, [token, refresh])
  useEffect(() => {
    const erp = sources.find(item => item.name === 'erp')
    if (!orderSource && sources.length) setOrderSource(erp?.id || sources[0].id)
  }, [sources, orderSource])

  const login = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    try { await refresh(tokenInput); sessionStorage.setItem('ontology_token', tokenInput); setToken(tokenInput) }
    catch (err) { setError((err as Error).message) }
  }
  const logout = () => { sessionStorage.removeItem('ontology_token'); setToken(''); setMe(null); setResult(null); setEvidence({}) }

  const investigate = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setNotice(''); setBusy(true); setEvidence({})
    try {
      const next = await call<Result>(token, '/v1/investigations', 'POST', {
        investigation_type: 'refund_eligibility', anchor: { source_system_id: orderSource, source_record_key: orderKey }, question,
      })
      setResult(next); setHistory(await call<History[]>(token, '/v1/investigations'))
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  const openInvestigation = async (id: string) => {
    setError(''); setEvidence({})
    try { setResult(await call<Result>(token, `/v1/investigations/${id}`)); setTab('investigate') }
    catch (err) { setError((err as Error).message) }
  }
  const openEvidence = async (id: string) => {
    try {
      const item = await call<Evidence>(token, `/v1/evidence/${id}`)
      setEvidence(old => ({ ...old, [id]: item }))
    }
    catch (err) { setError((err as Error).message) }
  }

  const submitAdmin = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(''); setNotice(''); setNewToken('')
    try {
      let path = ''; let body: object = {}
      switch (adminMode) {
        case 'source':
          path = '/v1/admin/sources'; body = { name: form.sourceName, max_age_seconds: Number(form.maxAge) }; break
        case 'entity':
          path = '/v1/admin/entities'; body = { class_name: 'Order', display_name: form.entityName, visibility: form.visibility }; break
        case 'property':
          path = '/v1/admin/properties'; body = { class_name: 'Order', name: form.propertyName,
            sensitivity: form.sensitivity, authority_source_system_id: form.sourceId }; break
        case 'binding':
          path = '/v1/admin/bindings'; body = { source_system_id: form.sourceId,
            record_key: form.recordKey, entity_id: form.entityId, method: 'manual' }; break
        case 'mapping':
          path = '/v1/admin/mappings'; body = { source_system_id: form.sourceId, target_class: 'Order',
            field_map: { [form.sourceField]: form.targetProperty }, version: 1 }; break
        case 'rule':
          path = '/v1/admin/rules'; body = { name: form.ruleName, applies_to: 'Order',
            expression: JSON.parse(form.expression), required_facts: form.requiredFacts.split(',').map(x => x.trim()),
            version: Number(form.version), effective_from: '2020-01-01T00:00:00Z' }; break
      }
      const response = await call<{ ingest_token?: string }>(token, path, 'POST', body)
      if (response.ingest_token) setNewToken(response.ingest_token)
      setNotice('已保存。规则创建后还需在规则列表中发布。')
      await refresh(token)
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  const publish = async (id: string) => {
    setError(''); setNotice('')
    try { await call(token, `/v1/admin/rules/${id}/publish`, 'POST'); setNotice('规则及语义包已发布。'); await refresh(token) }
    catch (err) { setError((err as Error).message) }
  }
  const ingest = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('')
    try {
      await call(token, '/v1/ingest/events', 'POST', {
        source_system_id: form.sourceId, record_key: form.recordKey, source_version: Number(form.eventVersion),
        valid_from: new Date().toISOString(), fields: { [form.sourceField]: form.eventValue }, deleted: false,
      })
      setNotice('事件已入队；Worker 处理后可刷新查看结果。')
      await refresh(token)
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }

  if (!me) return <div className="login-screen">
    <div className="login-panel">
      <div className="brand-icon large">◈</div>
      <div className="eyebrow">ONTOLOGY AGENT / WORKSPACE</div>
      <h1>读懂业务事实，<br /><em>解释每个判断。</em></h1>
      <p>使用管理员或分析员访问令牌进入工作台。租户与权限由服务端令牌决定。</p>
      <form onSubmit={login}><label>访问令牌</label><input autoFocus type="password" value={tokenInput}
        onChange={e => setTokenInput(e.target.value)} placeholder="输入 Bearer token" required />
        <button className="primary" type="submit">进入工作台 <span>↗</span></button></form>
      {error && <div className="alert error">{error}</div>}
      <div className="login-note">本地 Demo 的令牌见 README；生产环境请自行配置身份与凭据。</div>
    </div>
    <div className="login-art"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit orbit-three" />
      <div className="floating-card"><span className="tiny-dot" /> <small>FACT • SOURCE • RULE</small><strong>每一个结论<br />都有路径可循</strong></div>
      <span className="art-caption">BUSINESS SEMANTICS / INVESTIGATION RUNTIME</span></div>
  </div>

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-icon">◈</div><div><strong>Ontology</strong><small>Agent Workspace</small></div></div>
      <div className="side-label">工作空间</div>
      <nav>
        <button className={tab === 'investigate' ? 'active' : ''} onClick={() => setTab('investigate')}><span>⌕</span> 调查工作台</button>
        {me.role === 'admin' && <button className={tab === 'ontology' ? 'active' : ''} onClick={() => setTab('ontology')}><span>◇</span> 语义资产</button>}
        {me.role === 'admin' && <button className={tab === 'operations' ? 'active' : ''} onClick={() => setTab('operations')}><span>◷</span> 同步与审计</button>}
      </nav>
      <div className="side-spacer" />
      <div className="side-footer"><span className="online-dot" /> 只读调查运行中<br /><small>Tenant · {me.tenant_id}</small></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="breadcrumb">工作空间 <span>/</span> {tab === 'investigate' ? '调查工作台' : tab === 'ontology' ? '语义资产' : '同步与审计'}</div>
        <div className="account"><span className="avatar">{me.actor_id.slice(0, 1).toUpperCase()}</span><div><strong>{me.actor_id}</strong><small>{me.role}</small></div><button onClick={logout} title="退出">退出</button></div></header>
      <div className="content">
        {error && <div className="alert error" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}
        {notice && <div className="alert success" role="status">{notice}<button onClick={() => setNotice('')}>×</button></div>}
        {tab === 'investigate' && <>
          <div className="page-heading"><span className="eyebrow">INVESTIGATION / 01</span><h1>调查工作台</h1><p>输入业务对象，追溯事实与规则，获得明确或不确定的判断。</p></div>
          <div className="investigation-grid"><section className="card investigation-form"><div className="card-title"><div><span className="card-icon">⌕</span><strong>发起调查</strong></div><span className="quiet-tag">REFUND ELIGIBILITY</span></div>
            <form onSubmit={investigate}><div className="form-row"><div><label>来源系统</label><select value={orderSource} onChange={e => setOrderSource(e.target.value)} required>
              {sources.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select></div><div><label>来源订单号</label><input value={orderKey} onChange={e => setOrderKey(e.target.value)} required /></div></div>
              <label>调查问题</label><textarea rows={4} value={question} onChange={e => setQuestion(e.target.value)} maxLength={2000} />
              <div className="form-foot"><span>只读调查 · 当前权限内的事实</span><button className="primary" disabled={busy || !sources.length}> {busy ? '调查中…' : '开始调查'} <span>→</span></button></div>
            </form></section><section className="card intro-card"><div className="card-title"><strong>调查会检查什么</strong><span className="sparkle">✳</span></div>
              <div className="flow-step"><b>01</b><div><strong>确定对象身份</strong><p>按已确认的来源记录绑定找到订单。</p></div></div>
              <div className="flow-step"><b>02</b><div><strong>读取授权事实</strong><p>检查支付与发货事实的来源和时效。</p></div></div>
              <div className="flow-step"><b>03</b><div><strong>执行已发布规则</strong><p>输出逐项条件、证据与无法判断的原因。</p></div></div></section></div>
          {result && <section className="card result-card"><div className="card-title"><div><span className="card-icon">◇</span><strong>调查结果</strong></div><span className="muted">ID {short(result.investigation_id)}</span></div>
            <div className="result-head"><div><div className="eyebrow">BUSINESS DECISION</div><h2>{result.decision.kind === 'ELIGIBLE' ? '符合示例资格' : result.decision.kind === 'INELIGIBLE' ? '不符合示例资格' : result.decision.kind === 'CONFLICT' ? '事实存在冲突' : '暂时无法判断'}</h2><p>{result.display_message}</p></div><Badge value={result.decision.kind} /></div>
            <div className="metric-row"><div><span>执行状态</span><Badge value={result.execution.state} /></div><div><span>事实时效</span><strong>{result.quality.freshness}</strong></div><div><span>来源完整性</span><strong>{result.quality.source_completeness}</strong></div><div><span>语义包</span><strong>{short(result.semantic_bundle_id)}</strong></div></div>
            {!!result.decision.reason_codes.length && <div className="hint">原因：{result.decision.reason_codes.join(' · ')}</div>}
            <div className="result-columns"><div><h3>规则执行路径</h3><div className="trace-list">{renderTrace(result.rule_trace)}</div></div><div><h3>来源证据 <span className="count">{result.evidence_ids.length}</span></h3>
              {result.evidence_ids.map(id => <div className="evidence-row" key={id}><button onClick={() => openEvidence(id)}>查看证据 {short(id)} <span>↗</span></button>
                {evidence[id] && <div className="evidence-detail"><strong>{evidence[id].predicate}</strong><p>观察值：{JSON.stringify(evidence[id].observed_value)}</p><small>{evidence[id].source_record_key} · v{evidence[id].source_version} · {formatDate(evidence[id].observed_at)}</small></div>}</div>)}
              {result.quality.missing_required_facts.length > 0 && <div className="hint">待确认：{result.quality.missing_required_facts.join('、')}</div>}</div></div>
          </section>}
          <section className="card history-card"><div className="card-title"><strong>最近调查</strong><span className="count">{history.length}</span></div>
            {history.length === 0 ? <div className="empty">还没有调查记录</div> : <div className="history-list">{history.map(row => <button key={row.id} onClick={() => openInvestigation(row.id)}><span className="history-name">{row.entity_name}</span><Badge value={row.decision} /><time>{formatDate(row.created_at)}</time><span>↗</span></button>)}</div>}</section>
        </>}
        {tab === 'ontology' && admin && <><div className="page-heading"><span className="eyebrow">SEMANTIC GOVERNANCE / 02</span><h1>语义资产</h1><p>管理来源、属性、实体绑定与规则版本。每次变更都需确认其业务含义。</p></div>
          <div className="stats-grid"><div className="stat-card"><span>来源系统</span><strong>{admin.sources.length}</strong><small>已登记</small></div><div className="stat-card"><span>业务属性</span><strong>{admin.properties.length}</strong><small>受控定义</small></div><div className="stat-card"><span>规范实体</span><strong>{admin.entities.length}</strong><small>租户内</small></div><div className="stat-card"><span>已发布包</span><strong>{admin.active_bundle_id ? '01' : '00'}</strong><small>{short(admin.active_bundle_id)}</small></div></div>
          <div className="govern-grid"><section className="card"><div className="card-title"><strong>当前资产</strong><span className="quiet-tag">TENANT · {me.tenant_id}</span></div><h3>来源系统</h3>{admin.sources.map(s => <div className="asset-row" key={s.id}><span className="asset-mark">↗</span><div><strong>{s.name}</strong><small>最近同步：{formatDate(s.last_successful_sync_at)}</small></div><span className="muted">{short(s.id)}</span></div>)}
            <h3 className="section-gap">规则版本</h3>{admin.rules.map(r => <div className="asset-row" key={r.id}><span className="asset-mark violet">◇</span><div><strong>{r.name}</strong><small>v{r.version} · {short(r.id)}</small></div><Badge value={r.status} />{r.status === 'DRAFT' && <button className="text-button" onClick={() => publish(r.id)}>发布</button>}</div>)}
            <h3 className="section-gap">规范实体</h3>{admin.entities.map(e => <div className="asset-row" key={e.id}><span className="asset-mark teal">▣</span><div><strong>{e.display_name}</strong><small>{e.visibility} · {short(e.id)}</small></div></div>)}</section>
            <section className="card"><div className="card-title"><strong>配置资产</strong><span className="muted">管理员操作</span></div>
              <div className="segmented">{[['source','来源'],['entity','实体'],['property','属性'],['binding','绑定'],['mapping','映射'],['rule','规则']].map(([id,title]) => <button key={id} className={adminMode === id ? 'selected' : ''} onClick={() => setAdminMode(id)}>{title}</button>)}</div>
              <form className="config-form" onSubmit={submitAdmin}>{adminMode === 'source' && <><label>来源名称</label><input required value={form.sourceName} onChange={e => setField('sourceName', e.target.value)} placeholder="例如 erp" /><label>最大允许同步间隔（秒）</label><input required type="number" min="1" value={form.maxAge} onChange={e => setField('maxAge', e.target.value)} /></>}
                {adminMode === 'entity' && <><label>实体名称</label><input required value={form.entityName} onChange={e => setField('entityName', e.target.value)} placeholder="例如 ORDER-10002" /><label>访问范围</label><select value={form.visibility} onChange={e => setField('visibility', e.target.value)}><option value="shared">租户共享</option><option value="private">仅指定所有者</option></select></>}
                {adminMode === 'property' && <><label>属性名</label><input required value={form.propertyName} onChange={e => setField('propertyName', e.target.value)} placeholder="payment.status" /><SourceSelect sources={admin.sources} value={form.sourceId} onChange={v => setField('sourceId', v)} /><label>敏感等级</label><select value={form.sensitivity} onChange={e => setField('sensitivity', e.target.value)}><option value="internal">内部</option><option value="public">公开</option><option value="restricted">受限</option></select></>}
                {adminMode === 'binding' && <><SourceSelect sources={admin.sources} value={form.sourceId} onChange={v => setField('sourceId', v)} /><label>来源记录键</label><input required value={form.recordKey} onChange={e => setField('recordKey', e.target.value)} /><label>规范实体</label><select required value={form.entityId} onChange={e => setField('entityId', e.target.value)}><option value="">选择实体</option>{admin.entities.map(e => <option key={e.id} value={e.id}>{e.display_name}</option>)}</select></>}
                {adminMode === 'mapping' && <><SourceSelect sources={admin.sources} value={form.sourceId} onChange={v => setField('sourceId', v)} /><label>来源字段</label><input required value={form.sourceField} onChange={e => setField('sourceField', e.target.value)} /><label>目标属性</label><select required value={form.targetProperty} onChange={e => setField('targetProperty', e.target.value)}><option value="">选择属性</option>{admin.properties.map(p => <option key={p.name} value={p.name}>{p.name}</option>)}</select></>}
                {adminMode === 'rule' && <><label>规则名</label><input required value={form.ruleName} onChange={e => setField('ruleName', e.target.value)} /><label>版本</label><input required type="number" min="1" value={form.version} onChange={e => setField('version', e.target.value)} /><label>必需事实（逗号分隔）</label><input required value={form.requiredFacts} onChange={e => setField('requiredFacts', e.target.value)} /><label>受限规则 AST</label><textarea className="code-area" rows={8} value={form.expression} onChange={e => setField('expression', e.target.value)} /></>}
                <button className="primary" disabled={busy}>保存{adminMode === 'rule' ? '草稿' : '配置'} <span>→</span></button>
                {newToken && <div className="hint token-hint">来源令牌仅显示一次，请保存：<code>{newToken}</code></div>}</form></section></div></>}
        {tab === 'operations' && admin && <><div className="page-heading"><span className="eyebrow">OPERATIONS / 03</span><h1>同步与审计</h1><p>查看事件队列与执行状态，按登记的 Mapping 向来源系统提交事实。</p></div>
          <div className="govern-grid"><section className="card"><div className="card-title"><strong>提交来源事件</strong><span className="quiet-tag">ADMIN · TEST INPUT</span></div><p className="muted intro-text">供接入调试使用；正式来源应持自己的令牌调用 Ingest API，Worker 独立执行。</p><form className="config-form" onSubmit={ingest}><SourceSelect sources={admin.sources} value={form.sourceId} onChange={v => setField('sourceId', v)} /><label>来源记录键</label><input required value={form.recordKey} onChange={e => setField('recordKey', e.target.value)} /><label>来源版本</label><input required type="number" min="1" value={form.eventVersion} onChange={e => setField('eventVersion', e.target.value)} /><label>来源字段</label><input required value={form.sourceField} onChange={e => setField('sourceField', e.target.value)} /><label>字段值</label><input required value={form.eventValue} onChange={e => setField('eventValue', e.target.value)} /><button className="primary" disabled={busy}>事件入队 <span>→</span></button></form></section>
          <section className="card"><div className="card-title"><strong>最近任务</strong><button className="text-button" onClick={() => refresh(token)}>刷新 ↻</button></div>{admin.tasks.length ? admin.tasks.map(t => <div className="asset-row" key={t.id}><span className="asset-mark">◷</span><div><strong>{short(t.id)}</strong><small>{t.error_code || '无错误'}</small></div><Badge value={t.state} /></div>) : <div className="empty">队列为空</div>}</section></div></>}
      </div>
    </main>
  </div>
}

function SourceSelect({ sources, value, onChange }: { sources: Source[]; value: string; onChange: (value: string) => void }) {
  return <><label>来源系统</label><select required value={value} onChange={e => onChange(e.target.value)}><option value="">选择来源</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></>
}

function renderTrace(raw: unknown) {
  if (!raw || typeof raw !== 'object') return <div className="empty">没有规则路径</div>
  const trace = raw as { all?: { fact: string; result: string; fact_state: string }[]; result?: string }
  return (trace.all || []).map(item => <div className="trace-item" key={item.fact}><div><strong>{item.fact}</strong><small>事实状态 · {item.fact_state}</small></div><Badge value={item.result} /></div>)
}
