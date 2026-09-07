/* Direct image, video, and audio uploads from the Uploads gallery. */
(() => {
  const button = $('#uploads-page-button');
  const input = $('#uploads-page-input');
  if (!button || !input) return;

  const sync = () => {
    button.hidden = state.galleryView !== 'uploads' || !state.project || state.project.can_edit === false;
  };

  // Navigation and project loading can finish after the click handler runs.
  // Keep the action in sync with the gallery that actually gets rendered.
  const renderGalleryBeforeUploads = renderCurrentGallery;
  renderCurrentGallery = function (...args) {
    const result = renderGalleryBeforeUploads.apply(this, args);
    sync();
    return result;
  };
  sync();

  $$('.side-nav [data-gallery-view]').forEach(link => {
    link.addEventListener('click', () => requestAnimationFrame(sync));
  });

  input.onchange = async event => {
    const files = [...event.target.files];
    if (!files.length) return;
    if (!state.project || state.project.can_edit === false) {
      input.value = '';
      return alert('Select an editable project first.');
    }

    input.disabled = true;
    button.classList.add('busy');
    try {
      for (let index = 0; index < files.length; index += 1) {
        button.firstChild.nodeValue = `Uploading ${index + 1}/${files.length}…`;
        await uploadFile(files[index]);
      }
      const { assets } = await api(`/api/gallery?scope=mine&upload_refresh=${Date.now()}`);
      state.galleryAssets = assets;
      state.galleryView = 'uploads';
      renderCurrentGallery();
    } catch (error) {
      alert(`Upload failed: ${error.message}`);
    } finally {
      button.firstChild.nodeValue = '＋ Upload files';
      button.classList.remove('busy');
      input.disabled = false;
      input.value = '';
      sync();
    }
  };
})();
