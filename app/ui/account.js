const signup = window.location.pathname === '/signup';
const form = document.getElementById('account-form');
const submit = document.getElementById('account-submit');
const errorMessage = document.getElementById('account-error');
const password = document.getElementById('password');
const passwordToggle = document.getElementById('password-toggle');

passwordToggle.addEventListener('click', () => {
  const visible = password.type === 'password';
  password.type = visible ? 'text' : 'password';
  passwordToggle.setAttribute('aria-pressed', String(visible));
});
passwordToggle.hidden = false;

if (signup) {
  document.title = 'Create an account | EDIS';
  document.getElementById('account-title').textContent = 'Create an EDIS account';
  document.getElementById('account-prompt').textContent = 'Already have an account?';
  const link = document.getElementById('account-link');
  link.href = '/login';
  link.textContent = 'Sign in';
  password.autocomplete = 'new-password';
  password.minLength = 15;
  password.setAttribute('aria-describedby', 'password-hint');
  document.getElementById('password-hint').hidden = false;
  form.action = '/api/auth/signup';
  submit.textContent = 'Create account';
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  submit.disabled = true;
  form.setAttribute('aria-busy', 'true');
  errorMessage.hidden = true;
  try {
    const response = await fetch(form.action, {
      method: 'POST',
      body: new URLSearchParams(new FormData(form)),
    });
    if (response.ok) {
      window.location.replace('/workspace');
      return;
    }
    const { detail } = await response.json();
    errorMessage.textContent = typeof detail === 'string' ? detail : 'Check your username and password.';
  } catch {
    errorMessage.textContent = 'Could not reach EDIS. Please try again.';
  } finally {
    submit.disabled = false;
    form.removeAttribute('aria-busy');
  }
  errorMessage.hidden = false;
});
