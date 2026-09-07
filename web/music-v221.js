(function(){
  const previousSyncMode=window.syncMode;
  window.syncMode=function(){
    previousSyncMode();
    const mode=document.querySelector('.mode.active')?.dataset.mode;
    const music=document.querySelector('#music-panel');
    if(music)music.hidden=mode!=='music';
    document.querySelector('.composer')?.classList.toggle('music-mode',mode==='music');
    if(mode==='music'){
      document.querySelector('#tts-panel').hidden=true;
      document.querySelector('#tts-dialogue-heading').hidden=true;
      document.querySelector('#duration-wrap').hidden=false;
      document.querySelector('#ratio-wrap').hidden=true;
      document.querySelector('#resolution-wrap').hidden=true;
      document.querySelector('#media-row').hidden=true;
      const editor=document.querySelector('#prompt-editor');
      editor.hidden=false;
      editor.dataset.placeholder='Describe genre, mood, instruments and vocal style…';
      document.querySelectorAll('.creation-only').forEach(item=>item.hidden=true);
    }else{
      document.querySelector('#media-row').hidden=false;
    }
  };
  document.querySelectorAll('.mode').forEach(button=>button.addEventListener('click',()=>window.syncMode()));
  const previousPayload=window.buildGenerationPayload;
  window.buildGenerationPayload=async function(){
    const payload=await previousPayload();
    if(document.querySelector('.mode.active')?.dataset.mode==='music'){
      payload.ratio='N/A'; payload.resolution='Standard';
      payload.lyrics=document.querySelector('#music-lyrics').value.trim();
      payload.instrumental=document.querySelector('#music-instrumental').checked;
      payload.vocal_language=document.querySelector('#music-language').value;
      payload.bpm=Number(document.querySelector('#music-bpm').value)||0;
      payload.keyscale=document.querySelector('#music-key').value.trim();
    }
    return payload;
  };
  const filterOptions=document.querySelector('#filter-menu .filter-options');
  if(filterOptions&&!filterOptions.querySelector('[data-kind="audio"]')){
    const audioFilter=document.createElement('button');
    audioFilter.type='button';audioFilter.dataset.kind='audio';audioFilter.textContent='Audio';
    audioFilter.onclick=()=>{state.galleryFilter='audio';document.querySelectorAll('#filter-menu [data-kind]').forEach(item=>item.classList.toggle('active',item===audioFilter));document.querySelector('#filter-toggle').textContent='Filter · Audio';document.querySelector('#filter-toggle').classList.add('active');renderCurrentGallery()};
    filterOptions.append(audioFilter);
  }
  const previousRenderGallery=window.renderGallery;
  window.renderGallery=function(assets){
    previousRenderGallery(assets);
    [...document.querySelectorAll('#gallery .asset')].forEach((card,index)=>{
      if(assets[index]?.kind==='audio')card.style.gridRowEnd='span 14';
    });
  };
  document.querySelector('.mode[data-mode="music"]')?.addEventListener('click',()=>{
    state.galleryView='generations';state.galleryFilter='audio';
    document.querySelectorAll('.side-nav a').forEach(item=>item.classList.toggle('active',item.id==='nav-generations'));
    document.querySelectorAll('#filter-menu [data-kind]').forEach(item=>item.classList.toggle('active',item.dataset.kind==='audio'));
    document.querySelector('#filter-toggle').textContent='Filter · Audio';document.querySelector('#filter-toggle').classList.add('active');
    renderCurrentGallery();
  });
})();
