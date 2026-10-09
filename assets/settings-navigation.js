(()=>{'use strict';
 const labels={general:'通用',agents:'Agent 与模型',api:'API 连接',permissions:'工具权限',usage:'用量与预算',history:'运行历史',data:'数据与备份',about:'关于'};
 window.SettingsNavigation={init(mapping){
  const wrap=document.querySelector('.wrap'),head=wrap.querySelector('.head'),layout=document.createElement('div'),nav=document.createElement('nav'),content=document.createElement('main'),select=document.createElement('select'),buttons=new Map();
  layout.className='ah-settings-layout';nav.className='ah-settings-nav';nav.setAttribute('aria-label','设置导航');select.setAttribute('aria-label','设置分类');select.className='ah-settings-select';
  const cards=[...wrap.querySelectorAll(':scope>.card')],ids=Object.values(mapping).flat();
  if(cards.some(c=>!ids.includes(c.id))||new Set(ids).size!==ids.length)throw new Error('设置分类映射不完整');
  for(const [key,label] of Object.entries(labels)){if(!mapping[key])continue;const button=document.createElement('button');button.type='button';button.textContent=label;button.onclick=()=>choose(key);buttons.set(key,button);nav.append(button);const o=document.createElement('option');o.value=key;o.textContent=label;select.append(o)}
  head.after(select,layout);layout.append(nav,content);content.append(...cards);wrap.classList.add('ah-settings');select.onchange=()=>choose(select.value);
  function apply(){let hash='';try{hash=decodeURIComponent(location.hash.slice(1))}catch(_){}const category=mapping[hash]?hash:Object.keys(mapping).find(k=>mapping[k].includes(hash))||'general';
   for(const card of cards)card.hidden=!mapping[category].includes(card.id);
   for(const [key,b] of buttons)b.setAttribute('aria-current',key===category?'page':'false');select.value=category;
   if(ids.includes(hash))document.getElementById(hash).scrollIntoView({block:'start'});
  }
  function choose(key){if(location.hash==='#'+key)apply();else location.hash=key}
  window.addEventListener('hashchange',apply);apply();
 }};
})();
