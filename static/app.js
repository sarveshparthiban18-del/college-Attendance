document.addEventListener('DOMContentLoaded',()=>{
  const themeButtons=[...document.querySelectorAll('[data-theme-toggle]')];
  const applyTheme=(theme)=>{document.documentElement.dataset.theme=theme;try{localStorage.setItem('attendancepro-theme',theme);}catch(e){}themeButtons.forEach(button=>{const light=theme==='light';const icon=button.querySelector('.theme-toggle-icon');const label=button.querySelector('.theme-toggle-label');if(icon)icon.textContent=light?'☾':'☀';if(label)label.textContent=light?'Dark mode':'Light mode';button.setAttribute('aria-label',light?'Switch to dark mode':'Switch to light mode');});};
  let savedTheme='dark';try{savedTheme=localStorage.getItem('attendancepro-theme')||'dark';}catch(e){}applyTheme(savedTheme);
  themeButtons.forEach(button=>button.addEventListener('click',()=>applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light')));
  document.querySelectorAll('[data-toggle-password]').forEach(button=>button.addEventListener('click',()=>{const field=document.getElementById(button.dataset.togglePassword);if(!field)return;const showing=field.type==='text';field.type=showing?'password':'text';button.textContent=showing?'Show':'Hide';}));
  document.querySelectorAll('.flash-close').forEach(button=>button.addEventListener('click',()=>button.closest('.flash')?.remove()));
  document.querySelectorAll('form[data-confirm]').forEach(form=>form.addEventListener('submit',event=>{if(!window.confirm(form.dataset.confirm||'Are you sure?'))event.preventDefault();}));
  const clock=document.getElementById('live-clock');if(clock){const update=()=>{const now=new Date();clock.textContent=now.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' · '+now.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});};update();setInterval(update,60000);}
});
