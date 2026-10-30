'use strict';

// Synthetic fixtures only. This example never requests real memory or authenticates a user.
const memories = [
  {
    title: '项目联系与交付安排',
    level: 'S2 个人隐私',
    reveal: true,
    parts: [
      '项目交付请联系林予，电话 ',
      { label: '手机号已隐藏', original: '13800138000' },
      '。周五下午确认最终时间。',
    ],
  },
  {
    title: '测试接口配置',
    level: 'S3 凭据',
    reveal: true,
    parts: [
      '测试环境的 API Key 为 ',
      { label: 'API Key 已隐藏', original: 'sk-DEMO-ONLY-NOT-A-REAL-KEY' },
      '，仅用于本地联调。',
    ],
  },
  {
    title: '组织策略关闭原文查看',
    level: 'S2 个人隐私',
    reveal: false,
    parts: ['备用联系号码是 ', { label: '手机号已隐藏', original: '13900139000' }, '。'],
  },
  {
    title: '需求评审安排',
    level: 'S1 公开',
    reveal: false,
    parts: ['周三上午十点进行需求评审，请提前整理记忆检索的测试结果。'],
  },
];

const byId = id => document.getElementById(id);
const dialog = byId('verification');
let selected = 0;
let revision = 1;
let pending = null;
let revealed = false;
let notice = '';

function textElement(tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

function clearOriginal() {
  revealed = false;
  notice = '';
  pending = null;
}

function render() {
  const memory = memories[selected];
  const list = byId('memory-list');
  list.replaceChildren();
  memories.forEach((item, index) => {
    const button = textElement('button', '', 'memory');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(index === selected));
    button.append(textElement('strong', item.title));
    const preview = item.parts.map(part => typeof part === 'string'
      ? part : `[${part.label}]`).join('');
    button.append(textElement('span', preview));
    button.addEventListener('click', () => {
      clearOriginal();
      selected = index;
      revision += 1;
      render();
    });
    list.append(button);
  });

  byId('memory-title').textContent = memory.title;
  byId('level').textContent = memory.level;
  const content = byId('content');
  content.replaceChildren();
  memory.parts.forEach(part => {
    content.append(typeof part === 'string' ? document.createTextNode(part)
      : textElement('span', revealed ? part.original : part.label,
        revealed ? 'original' : 'hidden-part'));
  });

  const hasPrivateParts = memory.parts.some(part => typeof part !== 'string');
  byId('status').textContent = notice || (revealed ? '本次验证已通过，正在显示原文。'
    : hasPrivateParts ? '敏感内容已隐藏。' : '公开内容，无需还原。');
  byId('status').className = revealed ? 'status revealed' : 'status';
  byId('view').hidden = revealed || !memory.reveal;
  byId('clear').hidden = !revealed;
  byId('policy').textContent = !memory.reveal && hasPrivateParts
    ? '组织策略已关闭此类内容的原文查看。'
    : revealed ? '切换记忆、清除显示或离开页面时，原文会被清除。'
      : hasPrivateParts ? '每次查看单独验证，不向其他记忆授予原文查看权限。' : '';
}

byId('view').addEventListener('click', () => {
  pending = { selected, revision };
  dialog.showModal();
});

byId('approve').addEventListener('click', () => {
  if (pending && pending.selected === selected && pending.revision === revision) {
    revealed = true;
    notice = '';
  }
  pending = null;
  dialog.close();
  render();
});

byId('cancel').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => { pending = null; });

byId('changed').addEventListener('click', () => {
  clearOriginal();
  revision += 1;
  notice = '记忆已更新，请重新验证后查看最新内容。';
  dialog.close();
  render();
});

byId('clear').addEventListener('click', () => {
  clearOriginal();
  render();
});

window.addEventListener('pagehide', () => {
  clearOriginal();
  if (dialog.open) dialog.close();
  render();
});

render();
