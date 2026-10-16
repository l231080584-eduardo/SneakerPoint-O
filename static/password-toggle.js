document.querySelectorAll('[data-password-toggle]').forEach((toggle) => {
    const input = toggle.parentElement.querySelector('input[type="password"], input[type="text"]');
    const icon = toggle.querySelector('i');

    if (!input || !icon) return;

    toggle.addEventListener('click', () => {
        const isVisible = input.type === 'password';
        input.type = isVisible ? 'text' : 'password';
        toggle.setAttribute('aria-pressed', String(isVisible));
        toggle.setAttribute('aria-label', isVisible ? 'Ocultar contraseña' : 'Mostrar contraseña');
        toggle.title = isVisible ? 'Ocultar contraseña' : 'Mostrar contraseña';
        icon.classList.toggle('fa-eye', isVisible);
        icon.classList.toggle('fa-eye-slash', !isVisible);
    });
});
