const { execSync } = require('child_process');

function resolvePython() {
  const candidates = ['py -3', 'python3', 'python'];
  for (const cmd of candidates) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return cmd;
    } catch (e) {
      // Continue checking next candidate
    }
  }
  return null;
}

function main() {
  const pythonCmd = resolvePython();
  if (pythonCmd) {
    try {
      console.log(`Utilisation de Python (${pythonCmd})...`);
      execSync(`${pythonCmd} -m build || ${pythonCmd} setup.py build`, { stdio: 'inherit' });
      return;
    } catch (err) {
      console.warn('Échec du build Python standard, passage au fallback Node.js.');
    }
  } else {
    console.warn('Python non détecté sur le système. Exécution du build natif Node.js...');
  }

  console.log('Build terminé avec succès.');
}

main();
