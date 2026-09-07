(()=>{
  const editor=document.querySelector('#prompt-editor');
  if(!editor)return;
  const selector='.inline-mention,.inline-character-mention,.inline-video-mention';
  const asMention=node=>node?.nodeType===Node.ELEMENT_NODE&&node.matches(selector)?node:
    node?.parentElement?.closest(selector);

  function selectedMention(range){
    const direct=asMention(range.startContainer);
    if(direct&&editor.contains(direct))return direct;
    if(!range.collapsed){
      return [...editor.querySelectorAll(selector)].find(token=>range.intersectsNode(token))||null;
    }
    let node=range.startContainer,offset=range.startOffset;
    if(node.nodeType===Node.TEXT_NODE){
      const before=node.data.slice(0,offset);
      if(before.trim())return null;
      node=node.previousSibling||node.parentNode;
    }else node=node.childNodes[offset-1]||null;
    while(node?.nodeType===Node.TEXT_NODE&&!node.data.trim())node=node.previousSibling;
    return asMention(node);
  }

  function sync(){
    if(typeof syncReferencesFromEditor==='function')syncReferencesFromEditor();
    if(typeof syncCharactersFromPromptMentions==='function')syncCharactersFromPromptMentions();
    if(typeof syncPromptValue==='function')syncPromptValue();
    if(typeof renderMentionChips==='function')renderMentionChips();
    else if(typeof renderPromptMentionStrip==='function')renderPromptMentionStrip();
  }

  function remove(token){
    const parent=token.parentNode||editor,previous=token.previousSibling,next=token.nextSibling;
    token.remove();
    if(next?.nodeType===Node.TEXT_NODE&&!next.data.trim())next.remove();
    const range=document.createRange();
    if(previous?.isConnected){
      if(previous.nodeType===Node.TEXT_NODE)range.setStart(previous,previous.data.length);
      else range.setStartAfter(previous);
    }else{range.selectNodeContents(parent);range.collapse(false)}
    range.collapse(true);
    const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
    sync();
  }

  function intercept(event){
    if(event.isComposing)return;
    const selection=getSelection();
    let range=selection.rangeCount?selection.getRangeAt(0):null;
    const targetRange=event.getTargetRanges?.()[0];
    if(targetRange){range=document.createRange();range.setStart(targetRange.startContainer,targetRange.startOffset);range.setEnd(targetRange.endContainer,targetRange.endOffset)}
    if(!range||!editor.contains(range.startContainer))return;
    const token=selectedMention(range);
    if(!token||!editor.contains(token))return;
    event.preventDefault();event.stopImmediatePropagation();remove(token);
  }

  editor.addEventListener('beforeinput',event=>{
    if(event.inputType==='deleteContentBackward')intercept(event);
  },true);
  editor.addEventListener('keydown',event=>{
    if(event.key==='Backspace')intercept(event);
  },true);
})();
