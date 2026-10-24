const fs = require('node:fs');
const path = require('node:path');

const filePath = path.join(__dirname, 'recipes.json');
const dryRun = !process.argv.includes('--write');
const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
const quantityPattern = /((?:\d+\s*\/\s*\d+)|(?:\d+(?:[.,]\d+)?))(?:\s*-\s*((?:\d+\s*\/\s*\d+)|(?:\d+(?:[.,]\d+)?)))?/g;
const modifiers = '(?:(?:púpos|csapott|nagyobb|kisebb|nagy|kis|közepes|egész|friss|fagyasztott)\\s+)*';
const units = new RegExp(
  '^\\s*' + modifiers + '(?:g|kg|mg|ml|dl|cl|l|db|darab|ek|tk|tsk|gerezd|szelet|csomag|konzerv|adag|rúd|tojásfehérje|tojás|citrom|lime|tortilla|wrap|banán|zsemle|kifli|hagyma|paprika|paradicsom|alma|babérlevél|leveskocka|újhagyma|karika|bögre|pohár|fej|evőkanál|teáskanál|kanál|csipet|marék|golyó|gombóc)(?=$|[\\s.,;:)])',
  'i'
);

function numericValue(value) {
  const normalized = value.replace(/\s/g, '').replace(',', '.');
  if (normalized.includes('/')) {
    const [numerator, denominator] = normalized.split('/').map(Number);
    return numerator / denominator;
  }
  return Number(normalized);
}

function formattedValue(value) {
  return Number(value.toFixed(3)).toString().replace('.', ',');
}

function scaleIngredientText(text, factor) {
  if (factor === 1) return text;
  return text.split(/(https?:\/\/\S+)/gi).map((part) => {
    if (/^https?:\/\//i.test(part)) return part;
    return part.replace(quantityPattern, (quantity, first, second, offset, source) => {
    const rest = source.slice(offset + quantity.length);
    if (!units.test(rest)) return quantity;

    const scaledFirst = formattedValue(numericValue(first) * factor);
    if (second === undefined) return scaledFirst;
    const scaledSecond = formattedValue(numericValue(second) * factor);
    return `${scaledFirst}-${scaledSecond}`;
    });
  }).join('');
}

function scaleHtmlText(html, factor) {
  return html.replace(/>([^<]*)(?=<)/g, (match, text) =>
    `>${scaleIngredientText(text, factor)}`
  );
}

let changedRecipes = 0;
let changedIngredients = 0;
const examples = [];

for (const recipe of data.recipes) {
  const servings = Number(recipe.servings);
  const factor = 1 / servings;
  const originalIngredients = JSON.stringify(recipe.ingredients);

  if (Array.isArray(recipe.ingredients)) {
    recipe.ingredients = recipe.ingredients.map((ingredient) =>
      typeof ingredient === 'string' ? scaleIngredientText(ingredient, factor) : ingredient
    );
  } else if (typeof recipe.ingredients === 'string') {
    recipe.ingredients = scaleIngredientText(recipe.ingredients, factor);
  }

  if (typeof recipe.ingredients_html === 'string') {
    recipe.ingredients_html = scaleHtmlText(recipe.ingredients_html, factor);
  }

  if (JSON.stringify(recipe.ingredients) !== originalIngredients) {
    changedIngredients += 1;
    if (examples.length < 3) {
      examples.push({ name: recipe.name, ingredients: recipe.ingredients });
    }
  }

  const labelCalories = recipe.label?.match(/^\s*(\d+(?:[.,]\d+)?)\s*kcal/i);
  if (labelCalories) {
    recipe.calories_per_serving = Math.round(Number(labelCalories[1].replace(',', '.')));
  }

  recipe.servings = 1;
  if (servings !== 1) changedRecipes += 1;
}

if (!dryRun) {
  fs.writeFileSync(filePath, `${JSON.stringify(data, null, 4)}\n`);
}

console.log(JSON.stringify({
  mode: dryRun ? 'dry-run' : 'write',
  totalRecipes: data.recipes.length,
  recipesScaled: changedRecipes,
  recipesWithChangedIngredients: changedIngredients,
  allServingsAreOne: data.recipes.every((recipe) => recipe.servings === 1),
  examples
}, null, 2));