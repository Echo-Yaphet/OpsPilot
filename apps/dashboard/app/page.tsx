"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

const API = process.env.NEXT_PUBLIC_OPSPILOT_API ?? "http://localhost:8080";

type HealthItem = { name: string; healthy: boolean; detail?: Record<string, unknown> };
type SystemStatus = { healthy: boolean; services: HealthItem[]; infrastructure: HealthItem[] };
type AgentEvent = { agent: string; message: string; at: string };
type Evidence = { source: string; summary: string; data: unknown };
type Incident = {
  incident_id: string;
  service: string;
  symptom: string;
  status: string;
  root_cause: string;
  confidence: number;
  evidence: Evidence[];
  events: AgentEvent[];
  recommendations: { title: string; command?: string; risk: string; requires_approval: boolean }[];
  execution_result?: string;
  verified?: boolean;
};

const agentInitials: Record<string, string> = {
  coordinator: "CO", monitor: "ME", log: "LG", rca: "RC",
  solution: "SO", safety: "SA", executor: "EX", verification: "VE",
};

function timeLabel(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function evidenceKind(source: string) {
  if (source === "prometheus") return "METRIC";
  if (source.startsWith("llm_")) return "LOCAL LLM";
  if (source === "loki") return "LOG";
  return "EVIDENCE";
}

function llmDetail(item: Evidence) {
  if (!item.source.startsWith("llm_") || typeof item.data !== "object" || item.data === null) return null;
  const data = item.data as Record<string, unknown>;
  if (typeof data.rationale === "string") return data.rationale;
  if (typeof data.summary === "string") return data.summary;
  if (Array.isArray(data.steps)) return data.steps.filter((step) => typeof step === "string").join(" · ");
  if (typeof data.plan === "object" && data.plan !== null) {
    const rationale = (data.plan as Record<string, unknown>).rationale;
    if (typeof rationale === "string") return rationale;
  }
  return null;
}

function Signal({ healthy }: { healthy: boolean }) {
  return <span className={`signal ${healthy ? "healthy" : "critical"}`} aria-label={healthy ? "正常" : "异常"} />;
}

export default function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [response, incidentResponse] = await Promise.all([
        fetch(`${API}/api/v1/system/status`, { cache: "no-store" }),
        fetch(`${API}/api/v1/incidents?limit=20`, { cache: "no-store" }),
      ]);
      if (!response.ok || !incidentResponse.ok) throw new Error("无法读取系统状态");
      setStatus(await response.json());
      const history: Incident[] = await incidentResponse.json();
      setIncidents(history);
      setIncident((current) => history.find((item) => item.incident_id === current?.incident_id) ?? current ?? history[0] ?? null);
      setLastUpdated(new Date());
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "控制后端不可用");
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, 8000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [refresh]);

  async function analyze(execute = false) {
    setBusy(execute ? "execute" : "analyze");
    setError("");
    try {
      const response = await fetch(`${API}/api/v1/incidents/analyze`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ service: incident?.service ?? "payment-service", symptom: incident?.symptom ?? "dependency unavailable", execute, approved: execute, incident_id: execute ? incident?.incident_id : undefined }),
      });
      if (!response.ok) throw new Error("Agent 分析失败");
      setIncident(await response.json());
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "分析失败");
    } finally {
      setBusy(null);
    }
  }

  async function injectFault(fault: string, label: string) {
    if (!window.confirm(`确认注入「${label}」？这会暂时影响本地演示环境。`)) return;
    setBusy(fault);
    setError("");
    try {
      const response = await fetch(`${API}/api/v1/faults/${fault}`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ approved: true }),
      });
      if (!response.ok) throw new Error("故障注入失败");
      await new Promise((resolve) => window.setTimeout(resolve, fault === "cpu-spike" ? 500 : 3000));
      await refresh();
      if (fault !== "cpu-spike") await analyze(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "故障注入失败");
    } finally {
      setBusy(null);
    }
  }

  const allItems = useMemo(() => status ? [...status.services, ...status.infrastructure] : [], [status]);
  const healthyCount = allItems.filter((item) => item.healthy).length;
  const confidence = incident ? Math.round(incident.confidence * 100) : 0;

  return (
    <main>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><span /></div>
          <div><strong>OpsPilot</strong><small>智能运维控制台</small></div>
        </div>
        <div className="top-actions">
          <span className="environment"><i /> LOCAL · DOCKER</span>
          <span className="updated">{lastUpdated ? `${timeLabel(lastUpdated.toISOString())} 更新` : "正在连接"}</span>
          <button className="icon-button" onClick={refresh} aria-label="刷新系统状态">↻</button>
        </div>
      </header>

      <section className="shell">
        <div className="hero-row">
          <div>
            <p className="eyebrow">SYSTEM OVERVIEW</p>
            <h1>运维态势总览</h1>
            <p className="subtitle">从异常信号到根因与处置，让每一步都有证据。</p>
          </div>
          <button className="primary" onClick={() => analyze(false)} disabled={!!busy}>
            <span className="spark">✦</span>{busy === "analyze" ? "Agent 分析中…" : "启动 Agent 分析"}
          </button>
        </div>

        {error && <div className="error-banner"><strong>连接异常</strong><span>{error}</span><button onClick={refresh}>重试</button></div>}

        <section className="stats-grid">
          <article className="stat-card accent-card">
            <div className="stat-label"><span>整体健康度</span><span className="trend">实时</span></div>
            <div className="stat-value">{status ? `${Math.round((healthyCount / Math.max(allItems.length, 1)) * 100)}%` : "—"}</div>
            <div className="meter"><span style={{ width: status ? `${(healthyCount / Math.max(allItems.length, 1)) * 100}%` : "0%" }} /></div>
            <small>{healthyCount}/{allItems.length || 7} 个模块运行正常</small>
          </article>
          <article className="stat-card">
            <div className="stat-label"><span>活跃告警</span><span className={`pill ${status?.healthy ? "ok" : "warn"}`}>{status?.healthy ? "稳定" : "需关注"}</span></div>
            <div className="stat-value">{status ? allItems.length - healthyCount : "—"}</div>
            <small>{status?.healthy ? "当前没有未恢复异常" : "依赖或服务出现异常"}</small>
          </article>
          <article className="stat-card">
            <div className="stat-label"><span>分析置信度</span><span className="pill neutral">RCA</span></div>
            <div className="stat-value">{incident ? `${confidence}%` : "—"}</div>
            <small>{incident?.root_cause ?? "等待下一次 Agent 分析"}</small>
          </article>
          <article className="stat-card">
            <div className="stat-label"><span>平均处置时间</span><span className="pill ok">MVP</span></div>
            <div className="stat-value">&lt; 1m</div>
            <small>检测、分析、审批与验证闭环</small>
          </article>
        </section>

        <section className="main-grid">
          <div className="left-column">
            <article className="panel history-panel">
              <div className="panel-head"><div><p className="eyebrow">INCIDENT HISTORY</p><h2>事件记录</h2></div><span className="safe-label">可恢复</span></div>
              <div className="incident-list">
                {incidents.map((item) => (
                  <button className={item.incident_id === incident?.incident_id ? "selected" : ""} key={item.incident_id} onClick={() => setIncident(item)}>
                    <Signal healthy={item.status === "resolved" || item.status === "alert_resolved"} />
                    <span><strong>{item.service}</strong><small>{item.root_cause ?? item.symptom}</small></span>
                    <b>{item.status}</b>
                  </button>
                ))}
                {!incidents.length && <p className="history-empty">暂无持久化事件</p>}
              </div>
            </article>
            <article className="panel service-panel">
              <div className="panel-head"><div><p className="eyebrow">LIVE TOPOLOGY</p><h2>服务与依赖</h2></div><span className="live"><i /> LIVE</span></div>
              <div className="service-list">
                {allItems.map((item) => (
                  <div className="service-row" key={item.name}>
                    <div className={`service-icon ${item.name.includes("service") ? "app" : "infra"}`}>{item.name.slice(0, 2).toUpperCase()}</div>
                    <div className="service-copy"><strong>{item.name}</strong><span>{item.name.includes("service") ? "应用服务" : "基础设施"}</span></div>
                    <div className="service-status"><Signal healthy={item.healthy} /><span>{item.healthy ? "正常" : "异常"}</span></div>
                  </div>
                ))}
                {!status && [1, 2, 3, 4].map((n) => <div className="service-row skeleton" key={n} />)}
              </div>
            </article>

            <article className="panel drill-panel">
              <div className="panel-head"><div><p className="eyebrow">CHAOS LAB</p><h2>故障演练</h2></div><span className="safe-label">仅限本地环境</span></div>
              <p className="panel-intro">注入可恢复故障，观察告警、分析与处置闭环。</p>
              <div className="drill-list">
                <button onClick={() => injectFault("redis-down", "Redis 宕机")} disabled={!!busy}><span className="drill-icon redis">R</span><span><strong>Redis 宕机</strong><small>停止缓存依赖</small></span><b>运行 →</b></button>
                <button onClick={() => injectFault("mysql-down", "MySQL 连接异常")} disabled={!!busy}><span className="drill-icon mysql">M</span><span><strong>MySQL 连接异常</strong><small>停止数据库依赖</small></span><b>运行 →</b></button>
                <button onClick={() => injectFault("cpu-spike", "CPU 异常")} disabled={!!busy}><span className="drill-icon cpu">C</span><span><strong>CPU 异常</strong><small>15 秒有界负载</small></span><b>运行 →</b></button>
              </div>
            </article>
          </div>

          <div className="right-column">
            <article className={`panel incident-panel ${incident ? "has-incident" : ""}`}>
              <div className="panel-head">
                <div><p className="eyebrow">ACTIVE INCIDENT</p><h2>{incident ? "Agent 调查结果" : "当前事件"}</h2></div>
                {incident && <span className={`incident-state ${incident.status.includes("resolved") ? "resolved" : "open"}`}>{incident.status.includes("resolved") ? "已恢复" : "待处置"}</span>}
              </div>

              {!incident ? (
                <div className="empty-state"><div className="radar"><span /><i /></div><h3>系统正在持续观察</h3><p>启动 Agent 分析，或从左侧注入一个故障场景。</p></div>
              ) : (
                <>
                  <div className="rca-summary">
                    <div><span className="alert-icon">!</span><div><small>ROOT CAUSE</small><h3>{incident.root_cause}</h3></div></div>
                    <div className="confidence"><strong>{confidence}%</strong><span>置信度</span></div>
                  </div>

                  <div className="section-title"><span>证据链</span><small>{incident.evidence.length} 个来源</small></div>
                  <div className="evidence-grid">
                    {incident.evidence.map((item) => (
                      <div className="evidence-card" key={item.source}>
                        <span>{evidenceKind(item.source)}</span><strong>{item.source}</strong>
                        <p>{item.summary}</p>{llmDetail(item) && <p>{llmDetail(item)}</p>}
                      </div>
                    ))}
                  </div>

                  <div className="section-title"><span>Agent 协作时间线</span><small>{incident.events.length} 个步骤</small></div>
                  <div className="timeline">
                    {incident.events.map((event, index) => (
                      <div className="timeline-row" key={`${event.agent}-${event.at}`}>
                        <div className={`agent-avatar agent-${event.agent}`}>{agentInitials[event.agent] ?? "AG"}</div>
                        <div><div><strong>{event.agent}</strong><time>{timeLabel(event.at)}</time></div><p>{event.message}</p></div>
                        {index < incident.events.length - 1 && <i />}
                      </div>
                    ))}
                  </div>

                  {incident.recommendations[0] && (
                    <div className="recommendation">
                      <div className="recommend-head"><div><span>推荐处置</span><small className="risk">{incident.recommendations[0].risk} risk</small></div><code>{incident.recommendations[0].command}</code></div>
                      <p>{incident.recommendations[0].title}</p>
                      <button onClick={() => analyze(true)} disabled={!!busy || incident.status === "resolved"}>{busy === "execute" ? "执行并验证中…" : incident.status === "resolved" ? "处置已完成" : "批准执行并验证"}</button>
                    </div>
                  )}
                </>
              )}
            </article>
          </div>
        </section>
      </section>
    </main>
  );
}
