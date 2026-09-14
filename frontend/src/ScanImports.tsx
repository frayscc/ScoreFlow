import React from "react";

type PageRecord = {
  id:string; page_index:number; status:string; side?:"front"|"back"; sheet_number?:number;
  period_name?:string; period_status?:string; adopted:number; is_posted:number; effective_marks:number;
  predicted_delta:number; removed_at?:string; error?:string;
};
type ImportRecord = {
  id:string; asset_id?:string; import_job_id:string; original_filename:string; created_at:string; job_status:string; stage:string;
  total_pages:number; completed_pages:number; delete_requested:number; is_duplicate:number; pages:PageRecord[];
};
type Props = {
  projectId:string; csrf:string; onMessage:(message:string)=>void;
  onOpenReview:(id:string)=>void; onIdentify:(page:PageRecord)=>void; onPapersChanged:()=>Promise<void>;
};

const labels:Record<string,string> = { queued:"等待", processing:"识别中", needs_identity:"待确认身份", failed:"失败", ready:"已识别未确认", reviewed:"已确认", completed:"已完成", cancelled:"已取消" };

export default function ScanImports({ projectId, csrf, onMessage, onOpenReview, onIdentify, onPapersChanged }:Props) {
  const [records,setRecords] = React.useState<ImportRecord[]>([]);
  const [selected,setSelected] = React.useState<Set<string>>(new Set());
  const [filter,setFilter] = React.useState("all");
  const [includeRemoved,setIncludeRemoved] = React.useState(false);
  const [busy,setBusy] = React.useState(false);

  const request = React.useCallback(async (url:string, method="GET", body?:unknown) => {
    const response = await fetch(url,{method,headers:method === "GET" ? undefined : {"content-type":"application/json","x-scoreflow-csrf":csrf},body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(data.detail || "操作失败");
    return data;
  },[csrf]);
  const load = React.useCallback(async () => {
    if (!projectId) { setRecords([]); return; }
    setRecords(await request(`/api/projects/${projectId}/scan-imports?include_removed=${includeRemoved}`));
  },[projectId,includeRemoved,request]);
  React.useEffect(()=>{ void load(); const timer=window.setInterval(()=>void load(),2000); return ()=>window.clearInterval(timer); },[load]);

  const cancelAdoption = async (id:string) => {
    setBusy(true); try { await request(`/api/recognition-runs/${id}/adoption`,"DELETE"); await Promise.all([load(),onPapersChanged()]); onMessage("已取消该面确认，保留识别候选"); } finally { setBusy(false); }
  };
  const retry = async (id:string) => {
    setBusy(true); try { await request(`/api/recognition-runs/${id}/retry`,"POST",{}); await load(); onMessage("已加入重新识别队列"); } finally { setBusy(false); }
  };
  const removeSelected = async () => {
    const ids=Array.from(selected); if (!ids.length) return;
    setBusy(true);
    try {
      const preview=await request("/api/scan-records/removal-preview","POST",{run_ids:ids,reason:"预检"});
      const warning=preview.requires_reversal ? `\n其中需撤销 ${preview.requires_reversal} 张整表入账，影响 ${preview.affected_people} 人，净分值 ${Number(preview.score_delta)>=0?"+":""}${preview.score_delta}。` : "";
      if (!window.confirm(`将移除 ${preview.page_count} 个页面记录。${warning}\n删除当前采用页后，该面会恢复为待扫描，不会自动选用旧版本。`)) return;
      const reason=window.prompt("请输入移除原因","移除测试导入记录"); if (!reason) return;
      const result=await request("/api/scan-records","DELETE",{run_ids:ids,reason,reverse_posted:Boolean(preview.requires_reversal),idempotency_key:crypto.randomUUID()});
      setSelected(new Set()); await Promise.all([load(),onPapersChanged()]);
      onMessage(`已移除 ${result.page_count} 页${result.history_retained ? `，${result.history_retained} 页审计证据仅从活动列表隐藏` : ""}`);
    } finally { setBusy(false); }
  };
  const removeAsset = async (asset:ImportRecord) => {
    const wording=["queued","processing"].includes(asset.job_status) ? "取消识别并删除这次导入？" : `删除“${asset.original_filename}”及其未入账页面？`;
    if (!window.confirm(wording)) return;
    const reason=window.prompt("请输入删除原因","移除测试导入记录"); if (!reason) return;
    setBusy(true); try { const result=asset.asset_id ? await request(`/api/scan-assets/${asset.asset_id}`,"DELETE",{reason}) : await request(`/api/scan-jobs/${asset.import_job_id}/record`,"DELETE",{}); await load(); onMessage(result.status === "pending" ? "已请求在安全页边界取消并删除" : "导入记录已删除，可重新导入同一文件"); } finally { setBusy(false); }
  };

  const visible=records.map(record=>({...record,pages:record.pages.filter(page=>filter === "all" || (filter === "removed" ? page.removed_at : page.status === filter))})).filter(record=>filter === "all" || record.pages.length);
  return <section className="preview scan-imports">
    <div className="workbench-heading"><div><h2>导入记录</h2><p>按文件展开查看每页；删除扫描不会删除已签发纸表。</p></div><div className="actions"><select value={filter} onChange={event=>setFilter(event.target.value)}><option value="all">全部状态</option><option value="failed">失败</option><option value="ready">已识别未确认</option><option value="reviewed">已确认</option><option value="removed">已移除历史</option></select><label className="inline-check"><input type="checkbox" checked={includeRemoved} onChange={event=>setIncludeRemoved(event.target.checked)}/>显示历史</label><button onClick={()=>void load()}>刷新</button></div></div>
    <div className="actions"><button className="danger" disabled={!selected.size||busy} onClick={()=>void removeSelected().catch(error=>onMessage(error.message))}>移除选中 {selected.size} 页</button></div>
    {visible.map(record=><details key={record.import_job_id} open><summary><strong>{record.original_filename}</strong> · {record.is_duplicate?"重复文件（未再次处理）":labels[record.job_status]||record.job_status} · {record.completed_pages}/{record.total_pages||"?"}页 · {record.created_at}</summary>
      <div className="actions"><span>{record.stage}</span>{!record.is_duplicate&&<button className="danger inline" disabled={busy} onClick={()=>void removeAsset(record).catch(error=>onMessage(error.message))}>{["queued","processing"].includes(record.job_status)?"取消后删除":"删除本次导入"}</button>}</div>
      <table><thead><tr><th></th><th>页</th><th>周期/纸表</th><th>面别</th><th>状态</th><th>有效次数/影响</th><th>操作</th></tr></thead><tbody>{record.pages.map(page=><tr key={page.id} className={page.removed_at?"removed-row":""}><td><input type="checkbox" disabled={Boolean(page.removed_at)} checked={selected.has(page.id)} onChange={event=>setSelected(current=>{const next=new Set(current);event.target.checked?next.add(page.id):next.delete(page.id);return next;})}/></td><td>{page.page_index+1}</td><td>{page.period_name||"待匹配"}{page.sheet_number?` / 表${String(page.sheet_number).padStart(2,"0")}`:""}</td><td>{page.side==="front"?"正面":page.side==="back"?"背面":"—"}</td><td>{page.removed_at?"已从活动列表移除":page.is_posted?"已入账":labels[page.status]||page.status}{page.error&&<small>{page.error}</small>}{!page.removed_at&&["ready","reviewed"].includes(page.status)&&page.effective_marks===0&&<strong className="blank-warning">识别为全空白，请核对预览</strong>}</td><td>{page.effective_marks||0} / {page.predicted_delta>=0?"+":""}{page.predicted_delta||0}</td><td>{!page.removed_at&&<div className="actions wrap">{page.status==="needs_identity"?<button onClick={()=>onIdentify(page)}>输入短编号</button>:["ready","reviewed"].includes(page.status)?<button onClick={()=>onOpenReview(page.id)}>预览/复核</button>:<a href={`/api/recognition-runs/${page.id}/original`} target="_blank">查看原文件</a>}{!Boolean(page.adopted)&&["failed","ready","reviewed"].includes(page.status)&&<button disabled={busy} onClick={()=>void retry(page.id).catch(error=>onMessage(error.message))}>重新识别</button>}{Boolean(page.adopted)&&!Boolean(page.is_posted)&&<button onClick={()=>void cancelAdoption(page.id).catch(error=>onMessage(error.message))}>取消确认</button>}{Boolean(page.is_posted)&&<span>移除时将撤销整表</span>}</div>}</td></tr>)}</tbody></table>
    </details>)}
    {!visible.length&&<p>暂无符合条件的导入记录。</p>}
  </section>;
}
