(function(){
  'use strict';
  const HydroUI={
    isDark(){return document.documentElement.getAttribute('data-theme')==='dark';},
    refreshIcons(){if(window.lucide)window.lucide.createIcons({attrs:{'stroke-width':2}});},
    applyTheme(theme){
      const dark=theme==='dark';
      if(dark)document.documentElement.setAttribute('data-theme','dark'); else document.documentElement.removeAttribute('data-theme');
      try{localStorage.setItem('theme',dark?'dark':'light')}catch(_){}
      for(const iconId of ['themeIcon','themeHeaderIcon']){const icon=document.getElementById(iconId);if(icon)icon.setAttribute('data-lucide',dark?'sun':'moon');}
      this.refreshIcons();
      document.dispatchEvent(new CustomEvent('hydro:themechange',{detail:{theme:dark?'dark':'light'}}));
    },
    toggleTheme(){this.applyTheme(this.isDark()?'light':'dark');},
    enhanceFieldHelp(root=document){
      let tooltip=document.getElementById('fieldHelpTooltip');
      if(!tooltip){tooltip=document.createElement('div');tooltip.id='fieldHelpTooltip';tooltip.className='app-info-tooltip';tooltip.setAttribute('role','tooltip');document.body.appendChild(tooltip);}
      let active=null;
      const hide=()=>{tooltip.classList.remove('visible','below');active=null;};
      const show=(button)=>{
        const text=button.dataset.tooltip||button.closest('[data-help]')?.dataset.help||button.getAttribute('aria-label');
        if(!text)return;
        active=button;tooltip.textContent=text;tooltip.classList.add('visible');tooltip.classList.remove('below');
        const rect=button.getBoundingClientRect(),tip=tooltip.getBoundingClientRect(),margin=10;
        let left=rect.left+rect.width/2-tip.width/2;
        left=Math.max(8,Math.min(window.innerWidth-tip.width-8,left));
        let top=rect.top-tip.height-margin;
        if(top<8){top=rect.bottom+margin;tooltip.classList.add('below');}
        tooltip.style.left=`${left}px`;tooltip.style.top=`${top}px`;
        tooltip.style.setProperty('--tooltip-arrow-x',`${Math.max(12,Math.min(tip.width-12,rect.left+rect.width/2-left))}px`);
      };
      root.querySelectorAll('[data-help]').forEach(host=>{
        let button=host.querySelector('.info-tooltip,.field-help');
        if(!button)return;
        button.classList.remove('field-help');button.classList.add('info-tooltip');button.textContent='i';
        button.dataset.tooltip=host.dataset.help||button.getAttribute('title')||'';button.removeAttribute('title');
        if(!button.hasAttribute('tabindex'))button.tabIndex=0;
        if(button.dataset.helpEnhanced==='true')return;button.dataset.helpEnhanced='true';
        button.addEventListener('pointerenter',()=>show(button));button.addEventListener('pointerleave',hide);
        button.addEventListener('focus',()=>show(button));button.addEventListener('blur',hide);
        button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();active===button?hide():show(button);});
      });
      if(tooltip.dataset.bound!=='true'){
        tooltip.dataset.bound='true';
        document.addEventListener('pointerdown',event=>{if(!event.target.closest('.info-tooltip'))tooltip.classList.remove('visible','below');});
        window.addEventListener('resize',()=>tooltip.classList.remove('visible','below'),{passive:true});window.addEventListener('scroll',()=>tooltip.classList.remove('visible','below'),{passive:true,capture:true});
      }
    }
  };
  window.HydroUI=HydroUI;
  document.addEventListener('DOMContentLoaded',()=>{
    for(const iconId of ['themeIcon','themeHeaderIcon']){const icon=document.getElementById(iconId);if(icon)icon.setAttribute('data-lucide',HydroUI.isDark()?'sun':'moon');}
    for(const buttonId of ['themeToggleBtn','themeToggleHeaderBtn'])document.getElementById(buttonId)?.addEventListener('click',()=>HydroUI.toggleTheme());
    HydroUI.enhanceFieldHelp();HydroUI.refreshIcons();
  });
})();
