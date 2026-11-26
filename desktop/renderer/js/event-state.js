// Pure event projection. History pages share this state; rendering cannot own it.
// 主要引用或加载入口包括 `desktop/renderer/index.html`。
function initialEventState() {
  return {seq:0, ask:null, askStale:false, endStatus:'', status:''};
}
function reduceEventState(previous, event) {
  if (!validateProtocol('Event', event) || event.seq <= previous.seq) return previous;
  const state = {...previous, seq:event.seq};
  if (event.act === 'ask_human') {
    state.ask = event; state.askStale = false; state.endStatus = '';
  } else if (state.ask && event.seq > state.ask.seq &&
      ((event.act === 'contract' && event.ok) || event.act === 'tool' || event.act === 'say' ||
       (event.act === 'end' && event.status !== 'needs_human'))) state.askStale = true;
  if (event.act === 'end') state.endStatus = state.status = event.status || '';
  if (event.act === 'needs_human') state.status = 'needs_human';
  return state;
}
let _eventState = initialEventState();
function resetEventState() { _eventState = initialEventState(); }
