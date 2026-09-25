<script>
  import {tick} from 'svelte';
  export let title;
  export let open = false;
  export let closeLabel = 'Close';
  export let onClose = () => {};
  let dialog;
  let trigger;
  $: if (dialog) sync(open);
  async function sync(value) {
    if (value && !dialog.open) {trigger=document.activeElement;dialog.showModal();await tick();dialog.querySelector('button')?.focus();}
    else if (!value && dialog.open) dialog.close();
  }
  function closed() {onClose();trigger?.focus?.();}
</script>
<dialog bind:this={dialog} on:close={closed} on:cancel={onClose} aria-label={title}><div class="modal-head"><h2>{title}</h2><button class="icon-button" type="button" aria-label={closeLabel} on:click={onClose}>×</button></div><slot /></dialog>
