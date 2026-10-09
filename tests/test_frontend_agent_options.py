import os, sys, re, shutil, subprocess, tempfile
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_frontend():
    src=open(os.path.join(ROOT,'dashboard.html'),encoding='utf-8').read()
    assert 'id="sw-manager-agent"' in src and 'id="manager-options"' in src
    start=src.index('/* execution selection controls */')
    end=src.index('function mountAgentPicker',start)
    functions=src[start:end]
    node=shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    if not os.path.isfile(node):
        raise AssertionError('Node.js required for frontend behavior checks')
    script=r'''
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.style={};this.value='';this.disabled=false;}
  append(...els){this.children.push(...els)}
  appendChild(el){this.append(el);return el}
  replaceChildren(...els){this.children=els}
  setAttribute(k,v){this[k]=v}
}
global.document={createElement:tag=>new Element(tag)};
global.AsyncUI={run:async(button,label,operation)=>operation()};
global.view={tasks:new Map([['t',{type:'coding',intended_agent:'coder'}]])};
global.renderAll=()=>{};global.hint=()=>{};
''' + functions + r'''
function find(root,key){if(root.dataset.option===key)return root;for(const c of root.children){const got=find(c,key);if(got)return got;}return null;}
const a={name:'coder',kind:'claude_code',group:'Claude Code',model:'sonnet',model_choices:['sonnet','opus'],effort_supported:true,suggested_efforts:['low','high'],route_types:['coding'],allowed_tools:'Read,Edit'};
let current={model:'custom/model',effort:'high'};
const control=createExecutionControls(a,current,(next)=>{current=next});
const model=find(control,'model');model.value='custom';model.onchange();
if(current.effort!=='')throw Error('model change did not reset effort to Agent default');
const custom=find(control,'model-input');custom.value='new/model';custom.oninput();
if(current.model!=='new/model')throw Error('custom model was lost');
const unsupported=createExecutionControls({...a,kind:'codebuddy',effort_supported:false},{},()=>{});
if(find(unsupported,'effort'))throw Error('fake effort selector offered');
const mgr={...a,name:'manager',route_types:['manager'],allowed_tools:'Read'};
const card={agents:[mgr,a],overrides:{},execution_options:{}};
const picker=renderAgentPicker(card,'t');
const family=find(picker,'agent');family.value='claude_code';family.onchange();
if(card.overrides.t!=='coder')throw Error('manager permissions chosen for coding');
const strict={...a,model_capabilities:{sonnet:{effort_control:'supported',effort_levels:['low'],source:'configuration'},opus:{effort_control:'unsupported',effort_levels:[],source:'configuration'}}};
let strictValue={};
const strictControl=createExecutionControls(strict,{},next=>strictValue=next,{showSaveDefault:false});
const strictEffort=find(strictControl,'effort');
if(strictEffort.children.some(c=>c.textContent==='high'))throw Error('model-specific levels ignored');
const strictModel=find(strictControl,'model');strictModel.value='m1';strictModel.onchange();
if(!strictEffort.disabled)throw Error('unsupported model effort not disabled');
const unknownControl=createExecutionControls({...a,model_capabilities:{}},{model:'unknown'},()=>{}, {showSaveDefault:false});
if(!find(unknownControl,'capability-note').textContent.includes('未验证'))throw Error('unknown capability claimed supported');
let refreshed=0;
global.fetch=async()=>({ok:true,json:async()=>({})});
global.refreshAgentMetadata=async()=>{refreshed++};
(async()=>{
  const save=control.children.find(c=>c.textContent==='保存为此配置默认');
  await save.onclick();
  if(refreshed!==1)throw Error('saved defaults left manager metadata stale');
})();
console.log('Frontend execution choices passed');
'''
    with tempfile.TemporaryDirectory() as td:
        p=os.path.join(td,'test.js');open(p,'w',encoding='utf-8').write(script)
        result=subprocess.run([node,p],capture_output=True,text=True,encoding='utf-8')
        assert result.returncode==0,result.stdout+result.stderr
    scripts=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',src,re.S)
    with tempfile.TemporaryDirectory() as td:
        p=os.path.join(td,'full.js');open(p,'w',encoding='utf-8').write('\n'.join(scripts))
        result=subprocess.run([node,'--check',p],capture_output=True,text=True)
        assert result.returncode==0,result.stderr

if __name__=='__main__':
    test_frontend();print('Frontend agent options tests passed')
