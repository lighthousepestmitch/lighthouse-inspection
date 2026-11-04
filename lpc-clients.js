/* Client auto-fill, shared by Inspection Capture (index.html) and Service Records (records.html).
   Type part of a name or a street and the clients already on the books show under the box. The page decides what a tap fills in.
   It never forces a pick: a brand-new name, no signal, a logged-out phone or any error just means no list, and typing carries on as before.
   Mitch (master) reads the clients and client_properties tables directly, so his rows carry phone and email. A worker (tech) can only
   see their own jobs' clients, so they go through the database function client_lookup (~/lighthouse-supabase/38_client_lookup.sql),
   which only ever gives back name and address.
   Plain script: window.LPCClients = {search, attach, fullAddress}. Same search as the Job Calendar's name box (calendar.html searchClients). */
(function(){
"use strict";
const COLS="id,name,phone,email,street,suburb,state,postcode";
const trim=s=>String(s==null?"":s).trim();
const escLike=s=>String(s).replace(/[\\%_]/g,"\\$&");
const isPlaceholder=a=>{const t=trim(a);return !t||/^\(.*\)$/.test(t);};      // the Xero seed has "(address unknown)" rows
const mkAddr=(street,suburb,state,postcode)=>({street:isPlaceholder(street)?"":trim(street),suburb:trim(suburb),state:trim(state),postcode:trim(postcode)});
const hasAddr=a=>!!(a&&a.street);
// one line to show, and to spot the same place twice ("37 Beech Lane Wilsons Creek" against "37 Beech Lane" + suburb "Wilsons Creek")
function line(a){let s=a.street;if(a.suburb&&s.toLowerCase().indexOf(a.suburb.toLowerCase())<0)s+=(s?", ":"")+a.suburb;return s;}
function key(a){return line(a).toLowerCase().replace(/[^a-z0-9]+/g," ").replace(/\s+(nsw|qld|vic|tas|sa|wa|nt|act)(\s+\d{4})?$/,"").replace(/\s+\d{4}$/,"").trim();}
// "12 Smith St, Ballina NSW 2478": for a page with a single address box (Service Records)
function fullAddress(p){
  p=p||{};let s=trim(p.street);if(!s)return "";
  const sub=trim(p.suburb);if(sub&&s.toLowerCase().indexOf(sub.toLowerCase())<0)s+=", "+sub;
  const tail=[p.state,p.postcode].map(trim).filter(Boolean).join(" ");
  if(tail&&!/\d{4}\s*$/.test(s)&&s.toLowerCase().indexOf(tail.toLowerCase())<0)s+=" "+tail;
  return s;
}
// one client: addrs are their addresses, best first. The first becomes the item's own address and the rest are listed as sites.
// Phone and email are only copied for master, and only ever by name here, so a stray column can't leak through.
function item(c,master,addrs){
  const seen={},list=[];
  addrs.forEach(a=>{if(!hasAddr(a))return;const k=key(a);if(seen[k])return;seen[k]=1;list.push(a);});
  const a=list[0]||mkAddr("","","","");
  const o={id:String(c.id),name:trim(c.name),street:a.street,suburb:a.suburb,state:a.state,postcode:a.postcode,sites:list.slice(1)};
  if(master){o.phone=trim(c.phone);o.email=trim(c.email);}
  return o;
}

/* master: the tables directly. by="address" is for a street box: only matches on an address, one result per address. */
async function searchMaster(sb,q,by){
  const words=q.split(/\s+/).filter(Boolean).slice(0,4),digits=q.replace(/\D/g,""),byAddr=by==="address";
  const like=(b,col)=>{words.forEach(w=>{b=b.ilike(col,"%"+escLike(w)+"%");});return b;};
  const jobs=[],kinds=[],add=(k,b)=>{kinds.push(k);jobs.push(b);};
  if(byAddr){
    add("own",like(sb.from("clients").select(COLS),"street").order("name",{ascending:true}).limit(8));
  }else{
    add("name",like(sb.from("clients").select(COLS),"name").order("name",{ascending:true}).limit(8));
    if(digits.length>=3){
      // phones from the Xero seed are plain digits, phones typed in by hand may have spaces, so also try a looser pattern and keep only true digit matches
      add("phone",sb.from("clients").select(COLS).ilike("phone","%"+digits+"%").order("name",{ascending:true}).limit(8));
      if(digits.length>=6)add("phone2",sb.from("clients").select(COLS).ilike("phone","%"+digits.split("").join("%")+"%").order("name",{ascending:true}).limit(100));
    }
  }
  // job site addresses ("296 tyag", "paterson"), unless what was typed is clearly a phone number
  if((byAddr||q.length>=3)&&!(digits.length>=6&&/^[\d\s()+.-]+$/.test(q)))
    add("site",like(sb.from("client_properties").select("address,suburb,state,postcode,client:clients("+COLS+")"),"address").order("job_count",{ascending:false}).limit(12));
  const res=await Promise.all(jobs);
  const emb=row=>Array.isArray(row.client)?row.client[0]:row.client;
  if(byAddr){
    const out=[],seen={};
    res.forEach((r,i)=>{
      if(!r||r.error)return;       // e.g. the sites table isn't set up yet: the other half still shows
      (r.data||[]).forEach(row=>{
        const c=kinds[i]==="site"?emb(row):row;
        const a=kinds[i]==="site"?mkAddr(row.address,row.suburb,row.state,row.postcode):mkAddr(row.street,row.suburb,row.state,row.postcode);
        if(!c||!c.id||!hasAddr(a))return;
        const k=c.id+"|"+key(a);if(seen[k])return;seen[k]=1;
        out.push(item(c,true,[a]));
      });
    });
    return out.slice(0,10);
  }
  const seen={},prim=[],sec=[],site={};
  res.forEach((r,i)=>{
    if(!r||r.error)return;
    (r.data||[]).forEach(row=>{
      if(kinds[i]==="site"){
        const c=emb(row);if(!c||!c.id||seen[c.id]||isPlaceholder(row.address))return;
        seen[c.id]=1;site[c.id]=mkAddr(row.address,row.suburb,row.state,row.postcode);sec.push(c);return;
      }
      if(kinds[i]==="phone2"&&String(row.phone||"").replace(/\D/g,"").indexOf(digits)<0)return;
      if(!seen[row.id]){seen[row.id]=1;prim.push(row);}
    });
  });
  // name and phone matches first, but keep a few places for address matches so a street typed in isn't crowded out
  const room=Math.min(sec.length,3);
  const picked=prim.slice(0,8-room).concat(sec).slice(0,8);
  // their job sites, so a client with several places shows each one (the one that matched the typing goes first)
  const sites={};
  if(picked.length){
    const r=await sb.from("client_properties").select("client_id,address,suburb,state,postcode,job_count").in("client_id",picked.map(c=>c.id)).order("job_count",{ascending:false}).limit(150);
    if(r&&!r.error)(r.data||[]).forEach(p=>{(sites[p.client_id]=sites[p.client_id]||[]).push(mkAddr(p.address,p.suburb,p.state,p.postcode));});
  }
  return picked.map(c=>item(c,true,[site[c.id],mkAddr(c.street,c.suburb,c.state,c.postcode)].concat(sites[c.id]||[]).filter(Boolean)));
}

/* tech: the database function gives name and address only. One row per address, so group the rows by client. */
async function searchTech(sb,q,by){
  const r=await sb.rpc("client_lookup",{q:q});
  if(!r||r.error||!Array.isArray(r.data))return [];
  const byId={},order=[],out=[],seen={};
  r.data.forEach(row=>{
    if(!row||!row.id)return;
    const a=mkAddr(row.street,row.suburb,row.state,row.postcode);
    if(by==="address"){
      if(!hasAddr(a))return;
      const k=row.id+"|"+key(a);if(seen[k])return;seen[k]=1;
      out.push(item({id:row.id,name:row.name},false,[a]));return;
    }
    if(!byId[row.id]){byId[row.id]={c:{id:row.id,name:row.name},addrs:[]};order.push(row.id);}
    byId[row.id].addrs.push(a);
  });
  return by==="address"?out:order.map(id=>item(byId[id].c,false,byId[id].addrs));
}

/* search(q,{sb,master,by}) gives [{id,name,phone?,email?,street,suburb,state,postcode,sites:[{street,suburb,state,postcode}]}].
   Master needs 2 characters, a worker 3. Any trouble (offline, signed out, a missing table) gives [] and never throws. */
async function search(q,o){
  o=o||{};q=trim(q);
  try{
    if(!o.sb||q.length<(o.master?2:3))return [];
    return o.master?await searchMaster(o.sb,q,o.by):await searchTech(o.sb,q,o.by);
  }catch(e){return [];}
}

const CSS=
  ".lpcc-list{margin:6px 0 0;border:1px solid var(--edge,var(--line,#dcdcdc));border-radius:10px;overflow:hidden;max-height:60vh;overflow-y:auto;"
  +"background:var(--raised,var(--surface,#fff));box-shadow:var(--shadow,var(--raise,none))}"
  +".lpcc-list[hidden]{display:none}"
  +".lpcc-row{display:block;box-sizing:border-box;width:100%;min-height:44px;margin:0;padding:8px 12px;text-align:left;font:inherit;line-height:1.3;"
  +"border:0;border-bottom:1px solid var(--edge,var(--line,#dcdcdc));border-radius:0;background:none;color:var(--ink,#14201e);cursor:pointer;-webkit-appearance:none;appearance:none}"
  +".lpcc-row:last-child{border-bottom:0}"
  +".lpcc-row b{display:block;font-weight:600}"
  +".lpcc-row span{display:block;font-size:.88em;color:var(--muted,var(--ink2,#4b5b57));overflow-wrap:anywhere}"
  +".lpcc-row:active,.lpcc-row.on{background:var(--timber-soft,var(--surface2,#ddebec))}";
let styled=false,uid=0;
function addStyle(){if(styled)return;styled=true;const s=document.createElement("style");s.id="lpcc-style";s.textContent=CSS;document.head.appendChild(s);}

/* attach(inputEl,{getSb,isMaster,onPick,minChars,anchor,by}): getSb gives the Supabase client (or null when signed out; may be async),
   isMaster says which kind of search to run, onPick gets {id,name,phone?,email?,street,suburb,state,postcode} for the row tapped.
   anchor = the element the list goes under (default the box itself), by="address" for a street box. The list sits in the page flow under
   the box (like the street list already does), so nothing clips it. Safe to call twice on the same box. */
function attach(input,o){
  if(!input)return null;
  if(input.__lpcc)return input.__lpcc;
  o=o||{};addStyle();
  const id="lpcc-"+(++uid),byAddr=o.by==="address";
  let list=null,rows=[],active=-1,seq=0,timer=0,closeT=0,hold=false;
  if(!input.getAttribute("autocomplete"))input.setAttribute("autocomplete","off");      // the browser's own autofill mis-splits names
  input.setAttribute("role","combobox");input.setAttribute("aria-autocomplete","list");input.setAttribute("aria-expanded","false");
  function close(){
    rows=[];active=-1;
    if(list){list.hidden=true;list.textContent="";}
    input.setAttribute("aria-expanded","false");input.removeAttribute("aria-activedescendant");
  }
  function pick(i){
    const r=rows[i];if(!r)return;
    const p={id:r.it.id,name:r.it.name,street:r.a.street,suburb:r.a.suburb,state:r.a.state,postcode:r.a.postcode};
    if(r.it.phone!==undefined){p.phone=r.it.phone;p.email=r.it.email;}      // only master's rows have them
    seq++;clearTimeout(timer);close();
    try{if(o.onPick)o.onPick(p);}catch(e){try{console.error(e);}catch(x){}}
  }
  function mark(i){
    active=i;
    const kids=list?list.children:[];
    for(let k=0;k<kids.length;k++){const on=k===i;kids[k].classList.toggle("on",on);kids[k].setAttribute("aria-selected",on?"true":"false");}
    if(i>=0&&kids[i]){input.setAttribute("aria-activedescendant",kids[i].id);if(kids[i].scrollIntoView)kids[i].scrollIntoView({block:"nearest"});}
    else input.removeAttribute("aria-activedescendant");
  }
  function ensureList(){
    if(list)return list;
    const at=o.anchor||input;if(!at.parentNode)return null;
    list=document.createElement("div");list.className="lpcc-list";list.id=id;list.setAttribute("role","listbox");list.hidden=true;
    list.addEventListener("mousedown",e=>e.preventDefault());                 // desktop: keep the cursor in the box
    list.addEventListener("pointerdown",()=>{hold=true;clearTimeout(closeT);});
    const rel=()=>{hold=false;if(document.activeElement!==input){clearTimeout(closeT);closeT=setTimeout(close,350);}};
    list.addEventListener("pointerup",rel);list.addEventListener("pointercancel",rel);
    at.parentNode.insertBefore(list,at.nextSibling);
    return list;
  }
  // text only: names and addresses are typed in by people, so nothing goes through innerHTML
  function show(items){
    rows=[];
    items.forEach(it=>{
      [{street:it.street,suburb:it.suburb,state:it.state,postcode:it.postcode}].concat(byAddr?[]:it.sites||[]).slice(0,3).forEach(a=>rows.push({it:it,a:a}));
    });
    rows=rows.slice(0,8);
    const l=rows.length?ensureList():null;
    if(!l){close();return;}
    l.textContent="";
    rows.forEach((r,i)=>{
      const b=document.createElement("button"),t=document.createElement("b"),s=document.createElement("span"),ad=line(r.a);
      b.type="button";b.className="lpcc-row";b.id=id+"-"+i;b.setAttribute("role","option");b.setAttribute("aria-selected","false");
      if(byAddr){t.textContent=ad||r.it.name;s.textContent=[r.it.name,r.it.phone].filter(Boolean).join(" · ");}
      else{t.textContent=r.it.name;s.textContent=[ad,r.it.phone].filter(Boolean).join(" · ");}
      b.appendChild(t);if(s.textContent)b.appendChild(s);
      b.addEventListener("click",()=>pick(i));
      l.appendChild(b);
    });
    active=-1;l.hidden=false;input.setAttribute("aria-expanded","true");input.setAttribute("aria-controls",id);
  }
  async function run(q,master,my){
    let sb=null;
    try{sb=await (o.getSb&&o.getSb());}catch(e){sb=null;}
    if(my!==seq||!sb||!input.isConnected)return;
    const items=await search(q,{sb:sb,master:master,by:o.by});
    if(my!==seq||!input.isConnected||(document.activeElement&&document.activeElement!==input))return;      // typed on, moved away, or the box is gone
    show(items);
  }
  input.addEventListener("input",()=>{
    clearTimeout(timer);const my=++seq;                // anything still on its way is for older text
    const q=trim(input.value),master=!!(o.isMaster&&o.isMaster()),min=Math.max(o.minChars||0,master?2:3);
    if(q.length<min){close();return;}
    timer=setTimeout(()=>run(q,master,my),250);
  });
  input.addEventListener("keydown",e=>{
    if(!list||list.hidden||!rows.length)return;
    if(e.key==="ArrowDown"||e.key==="ArrowUp"){
      e.preventDefault();const d=e.key==="ArrowDown"?1:-1,n=rows.length;
      mark(active<0?(d>0?0:n-1):(active+d+n)%n);
    }
    else if(e.key==="Enter"&&active>=0){e.preventDefault();pick(active);}
    else if(e.key==="Escape"){e.preventDefault();seq++;clearTimeout(timer);close();}
  });
  input.addEventListener("blur",()=>{seq++;clearTimeout(timer);if(!hold){clearTimeout(closeT);closeT=setTimeout(close,200);}});
  return (input.__lpcc={close:close});
}

window.LPCClients={search:search,attach:attach,fullAddress:fullAddress};
})();
