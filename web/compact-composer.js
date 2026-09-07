(()=>{
  const row=document.querySelector('#media-row');
  const toggle=document.querySelector('#references-toggle');
  const summary=document.querySelector('#references-summary');
  if(!row||!toggle||!summary)return;
  let manuallyOpened=false,hoverOpened=false,hoverTimer=null;
  const activeMode=()=>document.querySelector('.mode.active')?.dataset.mode||'image';
  const sourceReady=()=>{
    const mark=document.querySelector('#source-media-mark');
    return Boolean((typeof state!=='undefined'&&state.sourceMedia)||mark?.querySelector('img,video')||mark?.textContent.trim()==='✓');
  };
  const referenceCount=()=>{
    let total=0;
    for(const id of ['reference-count','video-reference-count','audio-reference-count','video-audio-reference-count']){
      const value=parseInt(document.querySelector('#'+id)?.textContent||'0',10);
      if(Number.isFinite(value))total+=value;
    }
    if(sourceReady())total+=1;
    for(const id of ['start-frame-mark','end-frame-mark']){
      const mark=document.querySelector('#'+id);if(mark&&(mark.querySelector('img,video')||mark.textContent.trim()==='✓'))total+=1;
    }
    return total;
  };
  const render=(reset=false)=>{
    const mode=activeMode();
    const cinemaDock=document.querySelector('#cinema-dock');
    if(cinemaDock)cinemaDock.hidden=mode!=='cinema';
    if(reset)manuallyOpened=false;
    const required=['edit','upscale'].includes(mode)&&!sourceReady();
    const open=required||manuallyOpened||hoverOpened;
    row.classList.toggle('compact-collapsed',!open);
    toggle.classList.toggle('active',open);
    toggle.setAttribute('aria-expanded',String(open));
    const count=referenceCount();summary.textContent=String(count);summary.hidden=count===0;
    toggle.querySelector('span').textContent=['edit','upscale'].includes(mode)?'Source & refs':'Media & refs';
  };
  toggle.addEventListener('click',()=>{manuallyOpened=!manuallyOpened;hoverOpened=false;render()});
  toggle.addEventListener('mouseenter',()=>{clearTimeout(hoverTimer);hoverOpened=true;render()});
  const scheduleHoverClose=()=>{clearTimeout(hoverTimer);hoverTimer=setTimeout(()=>{hoverOpened=false;render()},260)};
  toggle.addEventListener('mouseleave',scheduleHoverClose);row.addEventListener('mouseenter',()=>clearTimeout(hoverTimer));row.addEventListener('mouseleave',scheduleHoverClose);
  document.querySelectorAll('.mode').forEach(button=>button.addEventListener('click',()=>setTimeout(()=>render(true),0)));
  row.addEventListener('change',()=>{manuallyOpened=true;setTimeout(()=>render(),0)});
  new MutationObserver(()=>render()).observe(row,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['hidden','src']});
  render(true);
})();


