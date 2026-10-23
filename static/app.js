"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, digits=1) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN",{maximumFractionDigits:digits});
const pct = v => v == null ? "—" : `${num(v)}%`;
const gib = v => v == null ? "—" : `${num(v / 1073741824)} GiB`;
const ms = v => v == null ? "—" : v < 1000 ? `${num(v,0)} ms` : `${num(v/1000,2)} s`;
const date = v => new Date(typeof v === "number" ? v*1000 : v).toLocaleString("zh-CN",{hour12:false});
const labels = {running:"进行中",success:"成功",error:"错误",cancelled:"已取消",interrupted:"已中断"};
let tab="overview", seconds=3600, offset=0, total=0, points=[], hover=null, pending=false;
async function api(path){const r=await fetch(path,{cache:"no-store"});if(!r.ok)throw Error(`HTTP ${r.status}`);return r.json();}
function text(id,value){$(id).textContent=value;}
function banner(value){$("banner").hidden=!value;text("banner",value||"");}

async function system(){
  const [{sample,error},models]=await Promise.all([api("/monitor/api/system"),api("/monitor/api/models")]);
  text("version",models.version ? `Ollama ${models.version}` : "Ollama 未连接");
  const warnings=[];if(models.error)warnings.push(`Ollama 状态暂不可用：${models.error}`);if(error)warnings.push(`系统采集异常：${error}`);
  if(sample){
    text("host",sample.hostname);text("updated",`采样于 ${new Date(sample.timestamp).toLocaleTimeString("zh-CN",{hour12:false})}`);
    if(Date.now()-Date.parse(sample.timestamp)>15000)warnings.push("系统采样已超过 15 秒未更新");
    text("cpu",pct(sample.cpu.percent));text("cpu-note",`${sample.cpu.physical_cores??"—"} 物理 / ${sample.cpu.logical_cores??"—"} 逻辑核心`);
    text("ram",pct(sample.memory.percent));text("ram-note",`${gib(sample.memory.used_bytes)} / ${gib(sample.memory.total_bytes)}`);
    const devices=sample.gpu.devices, utils=devices.map(d=>d.utilization_percent).filter(v=>v!=null);
    const mem=devices.filter(d=>d.memory_total_bytes!=null && d.memory_used_bytes!=null), used=mem.reduce((a,d)=>a+d.memory_used_bytes,0), capacity=mem.reduce((a,d)=>a+d.memory_total_bytes,0);
    text("gpu",utils.length ? pct(utils.reduce((a,b)=>a+b,0)/utils.length) : "—");text("gpu-note",devices.length ? `${devices.length} 张 NVIDIA GPU` : "未获得 GPU 指标");
    text("vram",capacity ? pct(used/capacity*100) : "—");text("vram-note",capacity ? `${gib(used)} / ${gib(capacity)}` : "显存不可用");
    $("devices").className="";
    $("devices").innerHTML=devices.length ? devices.map(d=>`<div class="device"><strong>${esc(d.name)}</strong><p>${gib(d.memory_used_bytes)} / ${gib(d.memory_total_bytes)} · ${num(d.temperature_c)} ℃ · ${num(d.power_watts)} W</p>${Object.keys(d.errors||{}).length?`<p>部分指标不可用：${esc(Object.keys(d.errors).join("、"))}</p>`:""}</div>`).join("") : `<div class="empty">${esc(sample.gpu.error||"没有可用的 GPU 指标")}</div>`;
    if(sample.gpu.status==="unavailable")warnings.push("GPU 采集暂不可用，CPU 和内存仍持续记录");
  }
  text("loaded-count",`${models.loaded.length} 个`);
  $("loaded").className="";
  $("loaded").innerHTML=models.loaded.length ? models.loaded.map(m=>`<div class="loaded-item"><strong>${esc(m.name)}</strong><p>${esc(m.details?.quantization_level)} · 上下文 ${num(m.context_length,0)} token · 模型显存 ${gib(m.size_vram)}<br>驻留至 ${esc(date(m.expires_at))}</p></div>`).join("") : '<div class="empty">当前没有已加载模型</div>';
  const select=$("model-filter"), previous=select.value, names=models.models.map(m=>m.name);
  if(select.dataset.names!==JSON.stringify(names)){select.innerHTML='<option value="">全部模型</option>'+names.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join("");select.value=previous;select.dataset.names=JSON.stringify(names);}
  $("health").className=`pill ${warnings.length?"bad":"good"}`;text("health",warnings.length?"需要关注":"监控在线");banner(warnings.join("；"));
}
async function history(){const r=await api(`/monitor/api/history?seconds=${seconds}`);points=r.items;text("resolution",r.resolution==="raw"?"原始采样":"分钟平均值");draw();}
function draw(){
  const canvas=$("chart"),box=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);
  const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);const w=box.width,h=box.height,L=35,R=12,T=14,B=28;
  ctx.font="11px Segoe UI";ctx.strokeStyle="#253347";ctx.fillStyle="#8192a8";
  for(let p=0;p<=100;p+=25){const y=T+(h-T-B)*(1-p/100);ctx.beginPath();ctx.moveTo(L,y);ctx.lineTo(w-R,y);ctx.stroke();ctx.fillText(String(p),3,y+4);}
  if(!points.length){ctx.fillText("等待历史采样数据",Math.max(L,w/2-55),h/2);return;}
  const t0=points[0].ts,t1=Math.max(t0+1,points[points.length-1].ts),x=t=>L+(w-L-R)*(t-t0)/(t1-t0),y=v=>T+(h-T-B)*(1-v/100);
  for(const [key,color] of [["cpu","#65a9ff"],["ram","#53d5bb"],["gpu","#bd9bff"],["vram","#ffc278"]]){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let started=false;for(const p of points){if(p[key]==null){started=false;continue;}if(started)ctx.lineTo(x(p.ts),y(p[key]));else{ctx.moveTo(x(p.ts),y(p[key]));started=true;}}ctx.stroke();if(points.length===1&&points[0][key]!=null){ctx.fillStyle=color;ctx.beginPath();ctx.arc(x(points[0].ts),y(points[0][key]),3,0,7);ctx.fill();}}
  ctx.fillStyle="#8192a8";ctx.fillText(new Date(t0*1000).toLocaleTimeString("zh-CN",{hour12:false}),L,h-5);ctx.textAlign="right";ctx.fillText(new Date(t1*1000).toLocaleTimeString("zh-CN",{hour12:false}),w-R,h-5);
  if(hover!=null){const target=t0+(t1-t0)*Math.min(1,Math.max(0,(hover-L)/(w-L-R)));const p=points.reduce((a,b)=>Math.abs(a.ts-target)<Math.abs(b.ts-target)?a:b);ctx.strokeStyle="#95adc180";ctx.beginPath();ctx.moveTo(x(p.ts),T);ctx.lineTo(x(p.ts),h-B);ctx.stroke();text("tooltip",`${date(p.ts)}\nCPU ${pct(p.cpu)}   内存 ${pct(p.ram)}\nGPU ${pct(p.gpu)}   显存 ${pct(p.vram)}`);$("tooltip").hidden=false;}
}
async function requests(){
  const q=new URLSearchParams({hours:$("hours-filter").value,limit:50,offset});if($("model-filter").value)q.set("model",$("model-filter").value);if($("state-filter").value)q.set("state",$("state-filter").value);
  const r=await api(`/monitor/api/requests?${q}`);total=r.total;text("request-total",`${total} 次请求`);text("page",`${Math.floor(offset/50)+1} / ${Math.max(1,Math.ceil(total/50))}`);$("previous").disabled=offset===0;$("next").disabled=offset+50>=total;
  $("request-rows").innerHTML=r.items.length?r.items.map(row=>`<tr data-id="${esc(row.id)}" tabindex="0"><td>${esc(date(row.started))}</td><td class="model" title="${esc(row.model)}">${esc(row.model)}</td><td><span class="badge ${Object.hasOwn(labels,row.state)?row.state:""}">${esc(labels[row.state]||row.state)}</span></td><td>${num(row.input_tokens,0)}</td><td>${num(row.output_tokens,0)}</td><td>${num(row.engine_tps,2)}</td><td>${ms(row.ttft_ms)}</td><td>${ms(row.duration_ms)}</td></tr>`).join(""):'<tr><td colspan="8" class="empty">这个范围内还没有请求。通过网关调用模型后，记录会出现在这里。</td></tr>';
}
async function summary(){const r=await api("/monitor/api/summary?hours=24");$("summary-rows").innerHTML=r.models.length?r.models.map(m=>`<tr><td class="model" title="${esc(m.model)}">${esc(m.model)}</td><td>${num(m.requests,0)}</td><td>${num(m.active,0)}</td><td>${num(m.succeeded,0)} / ${num(m.failed,0)}</td><td>${num(m.input_tokens,0)} / ${num(m.output_tokens,0)}</td><td>${num(m.engine_tps,2)}</td><td>${ms(m.avg_ttft_ms)}</td><td>${ms(m.avg_duration_ms)}</td></tr>`).join(""):'<tr><td colspan="8" class="empty">尚无模型调用记录</td></tr>';}
async function detail(id){const r=await api(`/monitor/api/requests/${encodeURIComponent(id)}`);const fields=[["模型",r.model],["状态",labels[r.state]||r.state],["生成速率",r.engine_tps==null?"—":`${num(r.engine_tps,2)} token/s`],["端到端速率",r.wall_tps==null?"—":`${num(r.wall_tps,2)} token/s`],["首字等待",ms(r.ttft_ms)],["总耗时",ms(r.duration_ms)],["输入 / 输出 token",`${num(r.input_tokens,0)} / ${num(r.output_tokens,0)}`],["加载耗时",ms(r.load_ms)],["统计完整",r.usage_complete?"是":"否 / 未获得最终 token 数"]];$("detail-summary").innerHTML=fields.map(([k,v])=>`<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("");text("detail-json",JSON.stringify(r,null,2));$("detail").showModal();}
function failed(e){$("health").className="pill bad";text("health","连接异常");banner(`无法更新监控数据：${e.message}`);}
async function refresh(){if(pending||document.hidden)return;pending=true;try{await system();if(tab==="overview")await history();else if(tab==="requests")await requests();else await summary();}catch(e){failed(e);}finally{pending=false;}}
document.querySelectorAll("nav button").forEach(button=>button.addEventListener("click",()=>{tab=button.dataset.tab;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active",b===button));document.querySelectorAll(".view").forEach(view=>view.hidden=view.id!==tab);refresh();}));
document.querySelectorAll(".range button").forEach(button=>button.addEventListener("click",()=>{seconds=Number(button.dataset.seconds);document.querySelectorAll(".range button").forEach(b=>b.classList.toggle("active",b===button));history().catch(failed);}));
for(const id of ["model-filter","state-filter","hours-filter"])$(id).addEventListener("change",()=>{offset=0;requests().catch(failed);});
$("refresh").addEventListener("click",()=>requests().catch(failed));$("previous").addEventListener("click",()=>{offset=Math.max(0,offset-50);requests().catch(failed);});$("next").addEventListener("click",()=>{offset+=50;requests().catch(failed);});
$("request-rows").addEventListener("click",event=>{const row=event.target.closest("tr[data-id]");if(row)detail(row.dataset.id).catch(failed);});$("request-rows").addEventListener("keydown",event=>{if(event.key==="Enter"){const row=event.target.closest("tr[data-id]");if(row)detail(row.dataset.id).catch(failed);}});
$("close-detail").addEventListener("click",()=>$("detail").close());$("chart").addEventListener("mousemove",event=>{hover=event.offsetX;draw();});$("chart").addEventListener("mouseleave",()=>{hover=null;$("tooltip").hidden=true;draw();});new ResizeObserver(draw).observe($("chart"));document.addEventListener("visibilitychange",refresh);
refresh();setInterval(refresh,2500);
