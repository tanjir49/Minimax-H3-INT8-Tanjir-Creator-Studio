(()=>{
  if(typeof renderMentionPicker!=='function'||typeof state==='undefined')return;
  let characterSelection=new Set();
  const selectionCount=()=>mentionSelection.size+characterSelection.size;
  const updateSelectionLabel=()=>{$('#mention-selection-count').textContent=`${selectionCount()} selected`};
  function characterMentionCard(character){
    const selected=characterSelection.has(character.id);
    const card=document.createElement('div');card.className=`mention-asset mention-character-card${selected?' selected':''}`;card.tabIndex=0;card.setAttribute('role','button');card.title='Click to select · Double-click to add and close';
    const preview=typeof characterPreview==='function'?characterPreview(character):'';
    if(preview){const image=document.createElement('img');image.src=preview;image.alt=character.name;image.loading='lazy';card.append(image)}else{const fallback=document.createElement('span');fallback.className='mention-character-fallback';fallback.textContent=character.name.slice(0,2).toUpperCase();card.append(fallback)}
    const name=document.createElement('span');name.textContent=`@${character.name}`;
    const source=document.createElement('small');source.className='character';const kind=character.kind==='prop'?'Prop':character.kind==='element'?'Element':'Character';source.textContent=`${kind} · ${character.references?.length||0} reference${character.references?.length===1?'':'s'}`;
    const check=document.createElement('b');check.textContent='✓';card.append(name,source,check);
    const toggle=()=>{if(characterSelection.has(character.id))characterSelection.delete(character.id);else characterSelection.add(character.id);card.classList.toggle('selected',characterSelection.has(character.id));updateSelectionLabel()};
    card.onclick=toggle;card.ondblclick=event=>{event.preventDefault();event.stopPropagation();if(!characterSelection.has(character.id))characterSelection.add(character.id);$('#apply-mentions').click()};card.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();toggle()}};return card;
  }
  renderMentionPicker=function(){
    const uploads=mentionAssets.filter(asset=>asset.source==='upload');const generated=mentionAssets.filter(asset=>asset.source!=='upload');
    $('#mention-character-count').textContent=state.characters.length;$('#mention-upload-count').textContent=uploads.length;$('#mention-generated-count').textContent=generated.length;
    $$('[data-mention-view]').forEach(button=>button.classList.toggle('active',button.dataset.mentionView===mentionGalleryView));
    const grid=$('#mention-grid');grid.innerHTML='';grid.classList.toggle('character-view',mentionGalleryView==='characters');
    if(mentionGalleryView==='characters'){
      if(!state.characters.length)grid.innerHTML='<p class="mention-empty">No saved characters yet. Create one from My Elements.</p>';else for(const character of state.characters)grid.append(characterMentionCard(character));
    }else{
      const shown=mentionGalleryView==='uploads'?uploads:generated;if(!shown.length)grid.innerHTML=`<p class="mention-empty">No ${mentionGalleryView} images yet.</p>`;for(const asset of shown)grid.append(mentionItemCard(asset));
    }
    updateSelectionLabel();
  };
  const openBase=openMentionPicker;
  openMentionPicker=async function(){mentionGalleryView='characters';characterSelection=new Set();return openBase()};
  $('#mention-trigger').onclick=()=>{captureMentionRange(false);openMentionPicker()};
  $$('[data-mention-view]').forEach(button=>button.onclick=()=>{mentionGalleryView=button.dataset.mentionView;renderMentionPicker()});
  $('#apply-mentions').onclick=()=>{
    for(const character of state.characters){if(!characterSelection.has(character.id))continue;if(!state.sceneCharacterIds.some(id=>String(id)===String(character.id)))state.sceneCharacterIds.push(character.id);ensureInlineCharacterMention(character,true)}
    for(const asset of mentionAssets){if(!mentionSelection.has(asset.id))continue;const existing=state.references.find(item=>String(item.assetId)===String(asset.id));if(existing)ensureInlineMention(existing,true);else appendReference({assetId:asset.id,filename:asset.filename,mime_type:asset.mime_type,url:asset.url,thumbnail_url:asset.thumbnail_url},'#reference-thumbs','#reference-count','references',9)}
    renderMentionChips();if(typeof updateCharacterSummary==='function')updateCharacterSummary();savedMentionRange=null;$('#mention-dialog').close();
  };
})();

