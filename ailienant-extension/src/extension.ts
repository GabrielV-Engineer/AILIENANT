import * as vscode from 'vscode';
import { IntentRouter } from './core/IntentRouter';
import { SessionManager } from './brain/session';

export function activate(context: vscode.ExtensionContext) {
	console.log('Congratulations, your extension "ailienant-extension" is now active!');

	const helloWorld = vscode.commands.registerCommand('ailienant-extension.helloWorld', () => {
		vscode.window.showInformationMessage('Hello World from ailienant-extension!');
	});

	const runTask = vscode.commands.registerCommand('ailienant-extension.runTask', async () => {
		const prompt = await vscode.window.showInputBox({
			prompt: 'Enter your directive for AILIENANT',
			placeHolder: 'e.g. "format", "constify", or describe a complex task',
		});
		if (!prompt) { return; }
		const doc = vscode.window.activeTextEditor?.document;
		const intercepted = await IntentRouter.intercept(prompt, doc);
		if (!intercepted) {
			await SessionManager.getInstance().startAITask(prompt);
		}
	});

	context.subscriptions.push(helloWorld, runTask);
}

export function deactivate() {}
