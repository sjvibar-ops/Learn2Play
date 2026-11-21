const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");
const source = readFileSync(join(__dirname, "../static/js/payments.js"), "utf8");

function form(dataset, elements) {
  const button = {disabled:false};
  return {dataset, elements, button, listeners:{},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    querySelectorAll() {return [button];}};
}

async function exercise({ending=false, failure=false, state="REFUNDED"}={}) {
  const elements = {};
  for (const name of ["state","reason","student_explanation","confirmation_notes","evidence_reference","authority_name","authority_capacity"]) {
    elements[name] = {value: name === "state" ? state : `DEMO ${name}`};
  }
  for (const name of ["independent_check_confirmed","funds_received_confirmed","authorized_refund_confirmed","account_holder_refund_confirmed","authorized_retention_confirmed"]) {
    elements[name] = {checked:name === "account_holder_refund_confirmed"};
  }
  const reconciliation = form({id:"9",version:"2"}, elements);
  const booking = form({bookingId:"7"}, {reason:{value:"DEMO booking ending reason"}});
  const feedback = {textContent:""};
  const requests = [];
  let reloads = 0;
  runInNewContext(source, {
    document: {
      getElementById: id => id === "paymentFeedback" ? feedback : null,
      querySelector: () => ({content:"DEMO-CSRF-TOKEN"}),
      querySelectorAll: selector => selector === ".payment-reconciliation-form" ? [reconciliation] : selector === ".admin-booking-end" ? [booking] : [],
    },
    window: {location:{reload(){reloads++;}}},
    fetch: async (url, options) => {
      requests.push({url,options});
      return {ok:!failure,json:async()=> failure ? {ok:false,message:"External confirmation is required."} : {ok:true}};
    },
  });
  const selected = ending ? booking : reconciliation;
  selected.listeners.submit({preventDefault(){},submitter:{value:"cancel"}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.method,"POST");
  assert.equal(requests[0].options.credentials,"same-origin");
  assert.equal(requests[0].options.headers["X-CSRFToken"],"DEMO-CSRF-TOKEN");
  assert.equal(requests[0].options.headers["Content-Type"],"application/json");
  if (ending) {
    assert.equal(requests[0].url,"/api/admin/bookings/7/cancel");
    assert.deepEqual(JSON.parse(requests[0].options.body),{reason:"DEMO booking ending reason"});
  } else {
    assert.equal(requests[0].url,"/api/admin/payments/9/reconcile");
    const body = JSON.parse(requests[0].options.body);
    assert.equal(body.expected_version,2);
    assert.equal(body.state,state);
    assert.equal(body.account_holder_refund_confirmed,true);
    assert.equal(body.authorized_retention_confirmed,false);
    assert.equal(body.student_explanation,"DEMO student_explanation");
    assert.equal(body.confirmation_notes,"DEMO confirmation_notes");
    assert.equal(body.authority_name,"DEMO authority_name");
  }
  assert.equal(reloads,failure ? 0 : 1);
  if (failure) {
    assert.equal(feedback.textContent,"External confirmation is required.");
    assert.equal(selected.button.disabled,false);
  }
}

(async()=>{
  await exercise();
  await exercise({failure:true});
  await exercise({ending:true});
})().catch(error=>{console.error(error);process.exitCode=1;});
