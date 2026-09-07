(()=>{
  if(typeof appendReference!=='function'||typeof state==='undefined')return;
  const originalAppendReference=appendReference;
  const urls=new WeakMap();
  let pinned=false,closeTimer=null;
  const imageUrl=item=>{if(item instanceof File){if(!urls.has(item))urls.set(item,URL.createObjectURL(item));return urls.get(item)}return item.thumbnail_url||item.url||''};
  const imageName=item=>item instanceof File?item.name:(item.filename||'Reference image');
  const release=item=>{if(item instanceof File&&urls.has(item)){URL.revokeObjectURL(urls.get(item));urls.delete(item)}};
  const manager=document.createElement('section');manager.id='reference-manager-popover';manager.className='reference-manager-popover';manager.hidden=true;manager.innerHTML='<header><div><b>Image references</b><small>Click Replace to upload a different photo</small></div><button type="button" aria-label="Close">×</button></header><div class="reference-manager-grid"></div>';
  document.body.append(manager);
  const grid=manager.querySelector('.reference-manager-grid');
  const close=()=>{manager.hidden=true;pinned=false};
  const scheduleClose=()=>{clearTimeout(closeTimer);closeTimer=setTimeout(()=>{if(!pinned)close()},260)};
  const cancelClose=()=>clearTimeout(closeTimer);
  manager.querySelector('header button').onclick=close;
  manager.onmouseenter=cancelClose;manager.onmouseleave=scheduleClose;
  addEventListener('keydown',event=>{if(event.key==='Escape'&&!manager.hidden)close()});
  addEventListener('pointerdown',event=>{if(!manager.hidden&&pinned&&!manager.contains(event.target)&&!event.target.closest('#reference-thumbs'))close()});
  function updateReferences(){
    $('#reference-count').textContent=`${state.references.length} / 9`;
    if(typeof renderMentionChips==='function')renderMentionChips();
    if(typeof renderPromptMentionStrip==='function')renderPromptMentionStrip();
    drawCompactThumbs();renderManager();
  }
  function removeAt(index){const item=state.references[index];if(!item)return;release(item);state.references.splice(index,1);updateReferences();if(!state.references.length)close()}
  function replaceAt(index,file){if(!file||!file.type.startsWith('image/'))return;const old=state.references[index];release(old);state.references[index]=file;updateReferences()}
  function renderManager(){
    grid.innerHTML='';
    state.references.forEach((item,index)=>{
      const card=document.createElement('article');
      const image=document.createElement('img');image.src=imageUrl(item);image.alt=imageName(item);
      const name=document.createElement('span');name.textContent=imageName(item);name.title=imageName(item);
      const actions=document.createElement('div');
      const replace=document.createElement('label');replace.textContent='Replace';replace.title='Upload a new image in this slot';
      const input=document.createElement('input');input.type='file';input.accept='image/*';input.onchange=()=>{const file=input.files?.[0];if(file)replaceAt(index,file);input.value=''};replace.append(input);
      const remove=document.createElement('button');remove.type='button';remove.className='reference-manager-remove';remove.textContent='×';remove.title='Remove this reference';remove.setAttribute('aria-label',`Remove ${imageName(item)}`);remove.onclick=()=>removeAt(index);
      actions.append(replace,remove);card.append(image,name,actions);grid.append(card);
    });
    if(!state.references.length)grid.innerHTML='<p>No image references selected.</p>';
  }
  function positionManager(){
    const anchor=$('#reference-thumbs').getBoundingClientRect();
    const width=Math.min(720,innerWidth-28);manager.style.width=`${width}px`;
    manager.style.left=`${Math.max(14,Math.min(innerWidth-width-14,anchor.left))}px`;
    manager.style.bottom=`${Math.max(76,innerHeight-anchor.top+8)}px`;
  }
  function openManager(lock=false){if(!state.references.length)return;clearTimeout(closeTimer);pinned=lock||pinned;renderManager();positionManager();manager.hidden=false}
  function drawCompactThumbs(){
    const thumbs=$('#reference-thumbs'),add=thumbs.querySelector('.reference-add');
    for(const node of [...thumbs.querySelectorAll('.reference-thumb,.reference-overflow-trigger')])node.remove();
    state.references.slice(0,3).forEach(item=>{
      const holder=document.createElement('button');holder.type='button';holder.className='reference-thumb';holder.dataset.referenceAssetId=item.assetId||'';holder.title='View, replace or remove references';
      const image=document.createElement('img');image.src=imageUrl(item);image.alt=imageName(item);
      const preview=image.cloneNode();preview.className='reference-preview';holder.append(image,preview);
      holder.onclick=event=>{if(event.isTrusted)openManager(true);else{const index=state.references.indexOf(item);if(index>=0)removeAt(index)}};
      thumbs.insertBefore(holder,add);
    });
    if(state.references.length>3){const more=document.createElement('button');more.type='button';more.className='reference-overflow-trigger';more.textContent=`+${state.references.length-3}`;more.title='Show all image references';more.onclick=()=>openManager(true);thumbs.insertBefore(more,add)}
    $('#reference-count').textContent=`${state.references.length} / 9`;
  }
  appendReference=function(item,thumbsSelector,countSelector,stateKey,max){
    if(stateKey!=='references')return originalAppendReference(item,thumbsSelector,countSelector,stateKey,max);
    if(state.references.length>=max)return alert(`Maximum ${max} references allowed.`);
    if(item.assetId&&state.references.some(value=>String(value.assetId)===String(item.assetId)))return alert('This gallery item is already selected.');
    state.references.push(item);if(item.assetId&&typeof ensureInlineMention==='function')ensureInlineMention(item);updateReferences();
  };
  const thumbs=$('#reference-thumbs');
  thumbs.addEventListener('mouseenter',()=>{if(state.references.length>3)openManager(false)});thumbs.addEventListener('mouseleave',scheduleClose);
  addEventListener('resize',()=>{if(!manager.hidden)positionManager()});
  drawCompactThumbs();
})();
