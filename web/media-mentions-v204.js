/* Canonical Characters / Uploads / Generated mention controller with video support. */
(() => {
  let characterSelection=new Set();
  const videoItem=asset=>({assetId:asset.id,filename:asset.filename,mime_type:asset.mime_type||'video/mp4',url:asset.url,thumbnail_url:asset.thumbnail_url});
  function insertVideoMention(item){
    const editor=$('#prompt-editor'),token=document.createElement('span');token.className='inline-video-mention';token.contentEditable='false';token.dataset.assetId=String(item.assetId);token.tabIndex=0;
    const index=state.videoRefs.findIndex(value=>String(value.assetId)===String(item.assetId));token.textContent=`@Video${index+1}`;
    const spacer=document.createTextNode(' ');let range=savedMentionRange;if(!range||!editor.contains(range.startContainer)){range=document.createRange();range.selectNodeContents(editor);range.collapse(false)}
    range.insertNode(spacer);range.insertNode(token);range.setStartAfter(spacer);range.collapse(true);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);syncPromptValue();renderPromptMentionStrip();
  }
  function assetCard(asset){
    const card=document.createElement('div');card.className=`mention-asset${mentionSelection.has(asset.id)?' selected':''}`;card.tabIndex=0;card.setAttribute('role','button');card.title='Click to select · Double-click to add';
    const media=document.createElement(asset.kind==='video'?'video':'img');media.src=asset.thumbnail_url||asset.url;if(media.tagName==='VIDEO'){media.muted=true;media.preload='metadata';media.playsInline=true}else{media.alt=asset.filename;media.loading='lazy'}
    const name=document.createElement('span');name.textContent=asset.filename;const source=document.createElement('small');source.textContent=`${asset.kind==='video'?'Video':'Image'} · ${asset.source==='upload'?'Uploaded':'Generated'}`;const check=document.createElement('b');check.textContent='✓';card.append(media,name,source,check);
    const toggle=()=>{mentionSelection.has(asset.id)?mentionSelection.delete(asset.id):mentionSelection.add(asset.id);renderMentionPicker()};card.onclick=toggle;card.ondblclick=e=>{e.preventDefault();mentionSelection.add(asset.id);$('#apply-mentions').click()};card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle()}};return card;
  }
  function characterCard(character){
    const card=document.createElement('div');card.className=`mention-asset mention-character-card${characterSelection.has(character.id)?' selected':''}`;card.tabIndex=0;card.setAttribute('role','button');const preview=characterPreview(character);
    if(preview){const image=document.createElement('img');image.src=preview;image.alt=character.name;card.append(image)}const name=document.createElement('span');name.textContent=`@${character.name}`;const source=document.createElement('small');source.textContent=character.kind==='prop'?'Prop':character.kind==='element'?'Element':'Character';const check=document.createElement('b');check.textContent='✓';card.append(name,source,check);
    const toggle=()=>{characterSelection.has(character.id)?characterSelection.delete(character.id):characterSelection.add(character.id);renderMentionPicker()};card.onclick=toggle;card.ondblclick=e=>{e.preventDefault();characterSelection.add(character.id);$('#apply-mentions').click()};return card;
  }
  renderMentionPicker=function(){
    const uploads=mentionAssets.filter(asset=>asset.source==='upload'),generated=mentionAssets.filter(asset=>asset.source!=='upload');$('#mention-character-count').textContent=state.characters.length;$('#mention-upload-count').textContent=uploads.length;$('#mention-generated-count').textContent=generated.length;
    $$('[data-mention-view]').forEach(button=>button.classList.toggle('active',button.dataset.mentionView===mentionGalleryView));const grid=$('#mention-grid');grid.innerHTML='';grid.classList.toggle('character-view',mentionGalleryView==='characters');
    if(mentionGalleryView==='characters'){if(!state.characters.length)grid.innerHTML='<p class="mention-empty">No saved characters yet.</p>';else for(const character of state.characters)grid.append(characterCard(character))}else{const shown=mentionGalleryView==='uploads'?uploads:generated;if(!shown.length)grid.innerHTML=`<p class="mention-empty">No ${mentionGalleryView} media yet.</p>`;else for(const asset of shown)grid.append(assetCard(asset))}
    $('#mention-selection-count').textContent=`${mentionSelection.size+characterSelection.size} selected`;
  };
  openMentionPicker=async function(){const mode=$('.mode.active').dataset.mode;if(!['image','video','cinema','edit'].includes(mode))return;if(!state.project)return alert('Select a project first.');try{const{assets}=await api(`/api/gallery?project_id=${encodeURIComponent(state.project.id)}`);mentionAssets=assets.filter(asset=>asset.kind==='image'||asset.kind==='video');mentionSelection=new Set();characterSelection=new Set();mentionGalleryView='uploads';renderMentionPicker();$('#mention-dialog').showModal()}catch(error){alert(error.message)}};
  $('#mention-trigger').onclick=()=>{captureMentionRange(false);openMentionPicker()};
  $$('[data-mention-view]').forEach(button=>button.onclick=()=>{mentionGalleryView=button.dataset.mentionView;renderMentionPicker()});
  $('#apply-mentions').onclick=()=>{
    const images=mentionAssets.filter(asset=>mentionSelection.has(asset.id)&&asset.kind==='image'),videos=mentionAssets.filter(asset=>mentionSelection.has(asset.id)&&asset.kind==='video');const newVideos=videos.filter(asset=>!state.videoRefs.some(item=>String(item.assetId)===String(asset.id)));
    if(images.filter(asset=>!state.references.some(item=>String(item.assetId)===String(asset.id))).length+state.references.length>9)return alert('Maximum 9 image references allowed.');if(newVideos.length+state.videoRefs.length>2)return alert('Maximum 2 video references allowed.');
    for(const character of state.characters.filter(item=>characterSelection.has(item.id))){if(!state.sceneCharacterIds.includes(character.id))state.sceneCharacterIds.push(character.id);ensureInlineCharacterMention(character,true)}
    for(const asset of images){const existing=state.references.find(item=>String(item.assetId)===String(asset.id));if(existing)ensureInlineMention(existing,true);else appendReference({assetId:asset.id,filename:asset.filename,mime_type:asset.mime_type,url:asset.url,thumbnail_url:asset.thumbnail_url},'#reference-thumbs','#reference-count','references',9)}
    for(const asset of videos){let item=state.videoRefs.find(value=>String(value.assetId)===String(asset.id));if(!item){item=videoItem(asset);appendReference(item,'#video-reference-thumbs','#video-reference-count','videoRefs',2)}insertVideoMention(item)}
    updateCharacterSummary();renderMentionChips();savedMentionRange=null;$('#mention-dialog').close();
  };
  const oldStrip=renderPromptMentionStrip;renderPromptMentionStrip=function(){oldStrip();const strip=$('#prompt-mention-strip');if(!strip)return;const seen=new Set();for(const chip of strip.children)seen.add(chip.textContent.trim());for(const token of $$('#prompt-editor .inline-video-mention')){const label=token.textContent.trim();if(seen.has(label))continue;const item=state.videoRefs.find(value=>String(value.assetId)===String(token.dataset.assetId));const chip=document.createElement('div');chip.className='prompt-mention-preview video';if(item?.thumbnail_url||item?.url){const media=document.createElement('video');media.src=item.thumbnail_url||item.url;media.muted=true;media.preload='metadata';chip.append(media)}const text=document.createElement('span');text.textContent=label;chip.append(text);strip.append(chip);seen.add(label)}strip.hidden=!strip.childElementCount};
})();
