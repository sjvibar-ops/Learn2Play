const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");
class Element {
  constructor(tag="div") { this.tag=tag; this.children=[]; this.dataset={}; this.listeners={}; this.attributes={}; this.textContent=""; this.value=""; this.files=[]; this.hidden=false; this.disabled=false; this.classes=new Set();
    this.classList={add:(...xs)=>xs.forEach(x=>this.classes.add(x)), remove:(...xs)=>xs.forEach(x=>this.classes.delete(x)), contains:x=>this.classes.has(x), toggle:(x,on)=>{on?this.classes.add(x):this.classes.delete(x)}};
  }
  append(...nodes){ this.children.push(...nodes); }
  appendChild(node){ this.append(node); return node; }
  replaceChildren(...nodes){ this.children=nodes; }
  setAttribute(k,v){ this.attributes[k]=v; }
  addEventListener(k,v){ this.listeners[k]=v; }
  focus(){ this.focused=true; }
  descendants(){ return this.children.flatMap(n=>[n,...n.descendants()]); }
}
const flush=()=>new Promise(setImmediate);
async function scenario(initialStatus) {
  const list=new Element(), mentor=new Element(), coach=new Element(), tab=new Element(), panel=new Element(), message=new Element(), retry=new Element();
  mentor.dataset.appRole="mentor"; coach.dataset.appRole="coach"; panel.classes.add("active");
  list.querySelectorAll=()=>[mentor,coach];
  const elements={roleApplicationsList:list,roleApplicationsMessage:message,roleApplicationsRetry:retry,"roleApp-mentor":mentor,"roleApp-coach":coach,"tab-roles":panel};
  const nodes=[]; let getFailure=false, submitted=false, requests=0, pending;
  const app=status=>({status,can_submit:status!=="pending",experience_description:"",links:[],credentials:[],review_note:status==="revision_required"?"<img src=x onerror=alert(1)>":null});
  const text=readFileSync(join(__dirname,"../static/js/settings.js"),"utf8");
  // Execute the actual provider module in isolation from unrelated settings forms.
  const source=text.slice(text.indexOf("// ---------- 9."));
  const events={};
  runInNewContext(source,{
    document:{ getElementById:id=>elements[id]||null, querySelector:()=>tab,
      createElement:tag=>{const el=new Element(tag);nodes.push(el);return el;},
      addEventListener:(name,fn)=>{events[name]=fn;}
    }, FormData, console, showSettingsToast(){},
    fetch:async (url,options)=>{
      if(url==="/api/csrf-token") return {ok:true,json:async()=>({csrf_token:"secure-token"})};
      if(url==="/api/me/applications") {
        if(getFailure) throw Error("offline");
        return {ok:true,json:async()=>({ok:true,applications:{mentor:app(submitted?"pending":initialStatus),coach:app("not_submitted")}})};
      }
      assert.equal(url,"/api/me/applications/mentor"); requests++;
      assert.equal(options.method,"POST"); assert.equal(options.headers["X-CSRFToken"],"secure-token");
      assert.equal(options.body.get("experience_description"),"A".repeat(70));
      assert.equal(options.body.has("status"),false);assert.equal(options.body.has("user_id"),false);
      return new Promise(resolve=>{pending=()=>{submitted=true;resolve({ok:true,status:201,json:async()=>({ok:true,message:"Submitted for review"})});};});
    }
  });
  await flush();
  assert.equal(list.attributes["aria-busy"],"false");
  if(initialStatus==="revision_required") assert(nodes.some(n=>n.textContent==="<img src=x onerror=alert(1)>"));
  const button=mentor.descendants().find(n=>n.tag==="button");
  assert.equal(button.textContent,initialStatus==="revision_required"?"Resubmit":"Apply");
  button.listeners.click();
  let form=mentor.descendants().find(n=>n.tag==="form");
  form.listeners.submit({preventDefault(){}});await flush();assert.equal(requests,0);
  form.descendants().find(n=>n.name==="experience_description").value="A".repeat(70);
  form.listeners.submit({preventDefault(){}});await flush();
  assert.equal(form.attributes["aria-busy"],"true");
  assert(form.descendants().find(n=>n.type==="submit").disabled);
  form.listeners.submit({preventDefault(){}});await flush();assert.equal(requests,1);
  pending();await flush();await flush();
  assert(mentor.descendants().some(n=>n.textContent==="Pending review"));
  assert(!mentor.descendants().some(n=>n.tag==="form"));
  assert(coach.descendants().some(n=>n.textContent==="Apply"));
  getFailure=true;await retry.listeners.click();await flush();
  assert.equal(retry.hidden,false);assert.match(message.textContent,/Could not load/);
  getFailure=false;await retry.listeners.click();await flush();
  assert.equal(retry.hidden,true);assert.equal(list.attributes["aria-busy"],"false");
  assert.equal(typeof events["l2:apply-provider"],"function");
}
(async()=>{await scenario("not_submitted");await scenario("revision_required");console.log("Provider application forms, resubmission, CSRF, busy states and retry passed.");})().catch(err=>{console.error(err);process.exitCode=1;});
