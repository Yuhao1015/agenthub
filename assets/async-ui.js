/* Scoped loading feedback. Errors remain the caller's responsibility. */
(()=>{'use strict';
 const active=new WeakMap();
 window.AsyncUI={run(button,label,operation){
  if(active.has(button))return active.get(button);
  const nodes=[...button.childNodes],disabled=button.disabled,busy=button.getAttribute('aria-busy');
  const spinner=document.createElement('span');spinner.className='ah-spinner';spinner.setAttribute('aria-hidden','true');
  const caption=document.createElement('span');caption.textContent=label;
  if(button.classList.contains('icon-only'))caption.className='ah-sr-only';
  button.replaceChildren(spinner,caption);button.disabled=true;button.setAttribute('aria-busy','true');
  const promise=Promise.resolve().then(operation).finally(()=>{button.replaceChildren(...nodes);button.disabled=disabled;if(busy===null)button.removeAttribute('aria-busy');else button.setAttribute('aria-busy',busy);active.delete(button)});
  active.set(button,promise);return promise;
 }};
})();
