import React from "react";

type PreviewRow = { student_number:string; name:string; source_line:number; status:"new"|"unchanged"|"name_difference"; existing_name?:string; existing_group_number?:number };
type Preview = { valid:boolean; count:number; recognized_49:boolean; rows:PreviewRow[]; errors:{line:number;text:string;message:string}[]; warnings:string[]; summary:{new:number;unchanged:number;name_differences:number} };
type Member = { student_id:string; student_number:string; name:string; group_number:number|null; sort_order:number };
type Grouping = { period_id:string; editable:boolean; revision:number|null; group_count:number; members:Member[]; leaders:Record<string,string|null> };

type Props = {
  projectId:string;
  periodId:string;
  groupCount:number;
  csrf:string;
  onMessage:(message:string) => void;
  onRosterChanged:() => Promise<void>;
};

const initialRoster = "学号 姓名\n1 张三\n2 李四";

export default function RosterGrouping({ projectId, periodId, groupCount, csrf, onMessage, onRosterChanged }:Props) {
  const [text, setText] = React.useState(initialRoster);
  const [preview, setPreview] = React.useState<Preview|null>(null);
  const [grouping, setGrouping] = React.useState<Grouping|null>(null);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [search, setSearch] = React.useState("");
  const [targetGroup, setTargetGroup] = React.useState(1);
  const [history, setHistory] = React.useState<Grouping[]>([]);
  const [busy, setBusy] = React.useState(false);

  const request = React.useCallback(async (url:string, method:string, body?:unknown) => {
    const response = await fetch(url, { method, headers:{ "content-type":"application/json", "x-scoreflow-csrf":csrf }, body:body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "操作失败");
    return data;
  }, [csrf]);

  const loadGrouping = React.useCallback(async () => {
    if (!periodId) { setGrouping(null); return; }
    const response = await fetch(`/api/periods/${periodId}/grouping`);
    if (response.ok) { setGrouping(await response.json()); setHistory([]); setSelected(new Set()); }
  }, [periodId]);

  React.useEffect(() => { void loadGrouping(); }, [loadGrouping]);
  React.useEffect(() => {
    if (!projectId || !text.trim()) { setPreview(null); return; }
    const timer = window.setTimeout(async () => {
      try { setPreview(await request(`/api/projects/${projectId}/roster/preview`, "POST", { text })); }
      catch (error) { onMessage(error instanceof Error ? error.message : "名单解析失败"); }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [text, projectId, request, onMessage]);

  const importRoster = async () => {
    setBusy(true);
    try {
      const result = await request(`/api/projects/${projectId}/roster/import`, "POST", { text });
      await onRosterChanged();
      await loadGrouping();
      onMessage(`名单已保存：新增${result.inserted}人，不变${result.unchanged}人，姓名差异保留${result.name_differences}人`);
    } finally { setBusy(false); }
  };

  const remember = () => { if (grouping) setHistory(items => [...items.slice(-19), structuredClone(grouping)]); };
  const move = (ids:string[], group:number|null, leaderId?:string) => {
    if (!grouping?.editable || !ids.length) return;
    remember();
    const moving = grouping.members.filter(member => ids.includes(member.student_id)).map(member => ({ ...member, group_number:group }));
    const remaining = grouping.members.filter(member => !ids.includes(member.student_id));
    const leaders = { ...grouping.leaders };
    for (const [number, id] of Object.entries(leaders)) if (id && ids.includes(id) && Number(number) !== group) leaders[number] = null;
    if (leaderId && group) leaders[String(group)] = leaderId;
    setGrouping({ ...grouping, members:[...remaining, ...moving], leaders });
    setSelected(new Set());
  };
  const reorderBefore = (movingId:string, targetId:string) => {
    if (!grouping?.editable || movingId === targetId) return;
    const moving = grouping.members.find(member => member.student_id === movingId);
    const target = grouping.members.find(member => member.student_id === targetId);
    if (!moving || !target || moving.group_number !== target.group_number) return;
    remember();
    const rows = grouping.members.filter(member => member.student_id !== movingId);
    rows.splice(rows.findIndex(member => member.student_id === targetId), 0, moving);
    setGrouping({ ...grouping, members:rows });
  };
  const setLeader = (group:number, studentId:string) => {
    if (!grouping?.editable) return;
    const member = grouping.members.find(item => item.student_id === studentId);
    if (member?.group_number !== group) { move([studentId], group, studentId); return; }
    remember(); setGrouping({ ...grouping, leaders:{ ...grouping.leaders, [String(group)]:studentId } });
  };
  const undo = () => {
    const previous = history.at(-1); if (!previous) return;
    setGrouping(previous); setHistory(items => items.slice(0, -1)); setSelected(new Set());
  };
  const save = async () => {
    if (!grouping?.editable || grouping.revision === null) return;
    setBusy(true);
    try {
      const result = await request(`/api/periods/${periodId}/grouping`, "PUT", {
        revision:grouping.revision,
        members:grouping.members.map(member => ({ student_id:member.student_id, group_number:member.group_number })),
        leaders:grouping.leaders,
      });
      setGrouping({ ...grouping, revision:result.revision }); setHistory([]); onMessage("分组草稿已保存，重启后仍会保留");
    } catch (error) {
      await loadGrouping();
      throw error;
    } finally { setBusy(false); }
  };

  const filtered = (members:Member[]) => members.filter(member => !search || member.student_number.includes(search) || member.name.includes(search));
  const renderMember = (member:Member, group:number|null) => <div className="student-chip" key={member.student_id} draggable={Boolean(grouping?.editable)}
    tabIndex={0} onDragStart={event => event.dataTransfer.setData("text/student-id", member.student_id)}
    onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); reorderBefore(event.dataTransfer.getData("text/student-id"), member.student_id); }}>
    {grouping?.editable && <input aria-label={`选择${member.name}`} type="checkbox" checked={selected.has(member.student_id)} onChange={event => setSelected(current => { const next=new Set(current); event.target.checked ? next.add(member.student_id) : next.delete(member.student_id); return next; })}/>}<span><strong>{member.student_number}</strong> {member.name}</span>
    {group !== null && grouping?.editable && <button className="text-button" onClick={() => setLeader(group, member.student_id)}>{grouping.leaders[String(group)] === member.student_id ? "组长" : "设为组长"}</button>}
  </div>;

  const total = grouping?.members.length || 0;
  const assigned = grouping?.members.filter(member => member.group_number !== null).length || 0;
  const expected = groupCount === 7 && total === 49 ? 7 : null;

  return <>
    <section className="roster-panel">
      <h2>2. 粘贴名单</h2>
      <p>一行一人，只需学号和姓名；支持空格、逗号、顿号和从 Excel 粘贴的 Tab。CSV 四列入口仍保留在下方高级区。</p>
      <textarea rows={9} value={text} onChange={event => setText(event.target.value)} placeholder="1 张三&#10;02 李四&#10;25号 王五"/>
      {preview && <div className={preview.valid ? "parse-summary good" : "parse-summary bad"}>
        <strong>{preview.recognized_49 ? "已识别49人" : `已解析 ${preview.count} 人`}</strong> · 新增 {preview.summary.new} · 不变 {preview.summary.unchanged} · 姓名差异 {preview.summary.name_differences}
        {preview.warnings.map(warning => <p key={warning}>提示：{warning}</p>)}
        {preview.errors.map(error => <p key={`${error.line}-${error.message}`}>第{error.line}行：{error.message}（{error.text || "空"}）</p>)}
      </div>}
      {preview?.rows.length ? <div className="roster-preview">{preview.rows.slice(0, 60).map(row => <div key={`${row.source_line}-${row.student_number}`}><span>{row.student_number}　{row.name}</span><span>{row.status === "new" ? "新增" : row.status === "unchanged" ? "不变" : `姓名差异：现有“${row.existing_name}”将保留`}</span></div>)}</div> : null}
      <button disabled={!preview?.valid || !preview.rows.length || busy} onClick={() => void importRoster().catch(error => onMessage(error.message))}>确认保存名单</button>
    </section>

    {periodId && grouping && <section className="grouping-workbench">
      <div className="workbench-heading"><div><h2>周期分组工作台</h2><p>{grouping.editable ? "当前为周期草稿，可移动并保存" : "周期已经开始，以下分组只读"} · 待分组 {total-assigned} 人 · 已分组 {assigned} 人</p></div><label>搜索学号或姓名<input value={search} onChange={event => setSearch(event.target.value)}/></label></div>
      {grouping.editable && <div className="group-tools"><span>已选 {selected.size} 人</span><select value={targetGroup} onChange={event => setTargetGroup(Number(event.target.value))}>{Array.from({length:groupCount},(_,i)=><option key={i+1} value={i+1}>移入第{i+1}组</option>)}</select><button disabled={!selected.size} onClick={() => move(Array.from(selected), targetGroup)}>批量移动</button><button disabled={!selected.size} onClick={() => move(Array.from(selected), null)}>返回待分组</button><button disabled={!history.length} onClick={undo}>撤销最近操作</button><button className="start" disabled={busy} onClick={() => void save().catch(error => onMessage(error.message))}>保存分组草稿</button></div>}
      <div className="grouping-layout">
        <aside className="student-pool" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); move([event.dataTransfer.getData("text/student-id")], null); }}><h3>待分组学生池 · {total-assigned}</h3>{filtered(grouping.members.filter(member => member.group_number === null)).map(member => renderMember(member, null))}</aside>
        <div className="group-cards">{Array.from({length:groupCount},(_,index)=>index+1).map(group => {
          const members = grouping.members.filter(member => member.group_number === group); const leader = grouping.leaders[String(group)];
          const problems = [...(!leader ? ["缺组长"] : []), ...(expected && members.length !== expected ? [members.length > expected ? "超员" : "缺员"] : [])]; const problem = problems.join("、") || "完整";
          return <article className={`group-card ${problem === "完整" ? "complete" : "needs-work"}`} key={group} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); move([event.dataTransfer.getData("text/student-id")], group); }}>
            <header><h3>第{group}组</h3><span>{members.length}人 · {problem}</span></header>
            <div className="leader-drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); const id=event.dataTransfer.getData("text/student-id"); move([id],group,id); }}>拖到这里设为组长：{members.find(member => member.student_id === leader)?.name || "未指定"}</div>
            {filtered(members).map(member => renderMember(member, group))}
          </article>;
        })}</div>
      </div>
    </section>}
    {projectId && !periodId && <section className="workflow"><p>名单保存后，请新建或选择一个周期草稿，再进入分组工作台。分组只属于该周期，不会修改已开始的历史周期。</p></section>}
  </>;
}
