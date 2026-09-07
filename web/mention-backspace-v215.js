(()=>{
  const editor=document.querySelector('#prompt-editor');
  if(!editor)return;
  const mentionSelector='.inline-mention,.inline-character-mention,.inline-video-mention';

  function previousMention(range){
    let node=range.startContainer;
    const offset=range.startOffset;
    if(node.nodeType===Node.TEXT_NODE){
      if(node.data.slice(0,offset).trim())return null;
      node=node.previousSibling;
    }else node=node.childNodes[offset-1]||null;
    while(node&&node.nodeType===Node.TEXT_NODE&&!node.data.trim())node=node.previousSibling;
    return node?.nodeType===Node.ELEMENT_NODE&&node.matches(mentionSelector)?node:null;
  }

  function removeMention(token){
    const parent=token.parentNode||editor;
    const next=token.nextSibling;
    if(next?.nodeType===Node.TEXT_NODE&&!next.data.trim())next.remove();
    token.remove();
    const range=document.createRange();
    range.selectNodeContents(parent);
    range.collapse(false);
    const selection=getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    if(typeof syncReferencesFromEditor==='function')syncReferencesFromEditor();
    if(typeof syncCharactersFromPromptMentions==='function')syncCharactersFromPromptMentions();
    if(typeof syncPromptValue==='function')syncPromptValue();
    if(typeof renderMentionChips==='function')renderMentionChips();
    if(typeof renderPromptMentionStrip==='function')renderPromptMentionStrip();
  }

  function handle(event){
    if(event.isComposing)return;
    const selection=getSelection();
    if(!selection.rangeCount||!selection.isCollapsed)return;
    const range=selection.getRangeAt(0);
    if(!editor.contains(range.startContainer))return;
    const token=previousMention(range);
    if(!token)return;
    event.preventDefault();
    removeMention(token);
  }

  editor.addEventListener('beforeinput',event=>{
    if(event.inputType==='deleteContentBackward')handle(event);
  });
  editor.addEventListener('keydown',event=>{
    if(event.key==='Backspace')handle(event);
  });
})();
