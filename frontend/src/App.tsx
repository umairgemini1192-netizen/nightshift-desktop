import { BrowserRouter } from "react-router-dom";
import { Shell } from "./app/Shell";

export function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}

export default App;
