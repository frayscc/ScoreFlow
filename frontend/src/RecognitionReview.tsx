import React from "react";

type Props = { review:any; csrf:string; onDecide:(slot:string,value:string)=>Promise<void>; onAddNote:()=>Promise<void>; onAdopt:()=>Promise<void>; onRemoved:()=>Promise<void>; onMessage:(message:string)=>void };

export default function RecognitionReview({review,csrf,onDecide,onAddNote,onAdopt,onRemoved,onMessage}:Props) {
  const [overlay,setOverlay] = React.useState(true);
  const [busy,setBusy] = React.useState(false);
  const counts = review.effective_counts || {};
  const markCount = (counts.slash_forward || 0) + (counts.slash_back || 0) + (counts.x || 0) + (counts.review || 0);
  const observations = review.observations.filter((item:any) => (item.manual_class || item.auto_class) !== "blank");
  const run = async (work:()=>Promise<void>) => { setBusy(true); try { await work(); } catch (error) { onMessage(error instanceof Error ? error.message : "操作失败"); } finally { setBusy(false); } };
  const remove = async () => {
    const reason = window.prompt("请输入移除此导入页面的原因"); if (!reason) return;
    const response = await fetch("/api/scan-records", {method:"DELETE",headers:{"content-type":"application/json","x-scoreflow-csrf":csrf},body:JSON.stringify({run_ids:[review.id],reason,reverse_posted:false})});
    const data = await response.json().catch(()=>({})); if (!response.ok) throw new Error(data.detail || "移除失败"); await onRemoved();
  };
  return <section className="preview review">
    <h2>实际识别栅格预览 · {review.side === "front" ? "正面" : "背面"}</h2>
    <p>下图是识别引擎实际使用的校正图，不是浏览器 PDF 预览。叠加标记同时使用颜色、形状和文字：<span className="legend slash">○ ／ 斜线</span> <span className="legend xmark">□ X 作废</span> <span className="legend uncertain">◇ ? 待复核</span></p>
    <label className="inline-check"><input type="checkbox" checked={overlay} onChange={event=>setOverlay(event.target.checked)}/>显示识别叠加层</label>
    <a href={overlay ? `/api/recognition-runs/${review.id}/overlay` : `/api/recognition-runs/${review.id}/corrected`} target="_blank"><img className="full-corrected-preview" src={overlay ? `/api/recognition-runs/${review.id}/overlay` : `/api/recognition-runs/${review.id}/corrected`} alt="识别引擎校正页"/></a>
    <p><a href={`/api/recognition-runs/${review.id}/original`} target="_blank">查看原始文件</a> · <a href={`/api/recognition-runs/${review.id}/notes-image`} target="_blank">查看纸面备注区</a> · 空白 {counts.blank || 0} · 斜线 {(counts.slash_forward || 0) + (counts.slash_back || 0)} · X {counts.x || 0} · 待复核 {counts.review || 0} · 预计影响 {review.effective_delta || 0} 分</p>
    {markCount === 0 && <div className="blank-confirm"><strong>识别为全空白，请核对上方实际预览。</strong><br/>系统不会自动采用或删除这张空白页，必须由教师明确确认。</div>}
    <div className="actions wrap"><button disabled={busy} onClick={()=>void run(onAddNote)}>录入可选备注</button><button className="danger" disabled={busy} onClick={()=>void run(remove)}>移除此导入页面</button></div>
    {review.notes?.map((note:any)=><p key={note.id}>电子备注：{note.note_text}</p>)}
    <p>点击任一非空槽位图片可放大；可直接修正为 1 空白、2 ／、3 ＼、4 X。</p>
    {observations.map((item:any)=><article key={item.slot_id}><a href={`/api/recognition-runs/${review.id}/slot/${item.slot_id}/image`} target="_blank"><img src={`/api/recognition-runs/${review.id}/slot/${item.slot_id}/image`} alt={`${item.student_name} ${item.rule_name} 第${item.slot_index}格`}/></a><div>{item.student_number}号 {item.student_name}<br/>{item.rule_name} · 第{item.slot_index}格<br/><small>当前：{item.manual_class || item.auto_class}</small></div><div className="actions"><button onClick={()=>void run(()=>onDecide(item.slot_id,"blank"))}>空白</button><button onClick={()=>void run(()=>onDecide(item.slot_id,"slash_forward"))}>／</button><button onClick={()=>void run(()=>onDecide(item.slot_id,"slash_back"))}>＼</button><button onClick={()=>void run(()=>onDecide(item.slot_id,"x"))}>X</button></div></article>)}
    <button className="start" disabled={busy || (counts.review || 0) > 0} onClick={()=>void run(onAdopt)}>{markCount === 0 ? "确认采用空白识别结果" : "采用该面复核结果"}</button>
  </section>;
}
