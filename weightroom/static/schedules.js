/* Keep each schedule's scope visible without changing its retained authority. */
(() => {
  const form=document.querySelector('[data-schedule-form]'); if(!form)return;
  const kind=form.querySelector('[name="kind"]'), node=form.querySelector('[name="machine"]'), model=form.querySelector('[name="model"]');
  const catalogue=JSON.parse(document.getElementById('schedule-catalogue').textContent);
  function show() {
    for(const group of form.querySelectorAll('[data-schedule-kind]')) {
      group.hidden=!group.dataset.scheduleKind.split(' ').includes(kind.value);
      for(const input of group.querySelectorAll('input,select'))input.disabled=group.hidden;
    }
    const selected=catalogue.find(row=>row.model_id===model.value), locations=selected?.locations || [];
    if(kind.value==='benchmark' && selected && locations.length) {
      const chosen=locations.find(one=>one.machine===node.value) || locations[0];
      form.querySelector('[data-schedule-target]').textContent='Target: '+chosen.machine+(chosen.machine!==node.value?' · Model absent on '+node.value+'; fallback shown here.':'.');
    }
  }
  for(const control of [kind,node,model])control.addEventListener('change',show);show();
})();
